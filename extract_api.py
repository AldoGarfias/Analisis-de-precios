# -*- coding: utf-8 -*-
"""EXTRACTOR TITULAR vía API de BI / Redshift — Aurora queda de SUPLENTE.

Decisión del usuario (2026-07-31), tras la validación espejo (+0.000% en 24
meses, historia muestreada e inventario): la fuente primaria de extracción es
la API (sin VPN); si una consulta falla, el día/semana cae automáticamente a
Aurora vía VPN (extract.py sigue siendo el respaldo directo completo).

Produce EXACTAMENTE los mismos parquets que extract.py:
  - data/reporte61/ventas_YYYY_MM.parquet   (renglones crudos, filtros del extract)
  - data/reporte61/existencias_sem.parquet  (snapshot semanal por código, lunes)

Mecánica API: renglones por PAGINACIÓN OFFSET (el servidor tope 1000 filas;
verificado 2026-07-31 que ORDER BY + OFFSET pagina sin solaparse). Redshift
regresa TODO como texto: se castea aquí para replicar el esquema de Aurora.
'disp_venta' usa el cache data/cat_almacen_vendible.json (cat_almacen solo
vive en Aurora; se refresca oportunista cuando hay VPN).

Normalización de texto (regla de migración 2026-07-31): NULL→'' — y NUNCA
filtrar por caracteres acentuados (el ETL de IT los pierde: 'línea'→'l?nea').

Uso:  ./.venv/bin/python extract_api.py                 # ventas (meses faltantes/parciales)
      ./.venv/bin/python extract_api.py existencias    # snapshots semanales faltantes
"""
import json
import os
import sys
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_bi import q  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data", "reporte61")

COLS = ["fecha", "folio", "codigo", "codigo_cliente", "cantidad", "precio",
        "tipo_precio", "descuento_uno", "descuento", "desc_fin", "subtotal",
        "costo_venta", "tipo_cambio", "concepto", "clasificacion_descuento",
        "via", "kit"]
_NUM = ["cantidad", "precio", "tipo_precio", "desc_fin", "subtotal",
        "costo_venta", "tipo_cambio"]
_TXT = [c for c in COLS if c not in _NUM and c != "fecha"]
F_API = ("estatus='Activa' AND CAST(precio AS DECIMAL(18,4))>0 "
         "AND CAST(cantidad AS DECIMAL(18,4))>0")
PAGINA = 1000
PAUSA = 2.1


def _norm(df):
    """Replica el esquema lógico de la extracción de Aurora."""
    d = df.copy()
    f = pd.to_datetime(d.fecha)
    if f.dt.tz is not None:
        f = f.dt.tz_localize(None)
    d["fecha"] = f
    for c in _NUM:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["tipo_precio"] = d.tipo_precio.fillna(0).astype(int)
    for c in _TXT:
        d[c] = d[c].fillna("").astype(str)
    # caracteres invisibles en códigos (caso PS1212F1 con U+200B, 2026-07-31):
    # Aurora los entrega como mojibake y la API como unicode real — se limpian
    # para que el código sea idéntico venga de donde venga
    d["codigo"] = d.codigo.str.replace("​", "", regex=False) \
                          .str.replace("â€‹", "", regex=False).str.strip()
    return d[COLS]


def ventas_dia_api(fecha):
    """Renglones de UN día vía API, paginados; verifica contra COUNT."""
    sel = ", ".join(COLS)
    w = (f"CAST(fecha AS DATE) = DATE '{fecha}' AND {F_API}")
    total = int(q(f"SELECT COUNT(*) AS n FROM reporte_61 WHERE {w} LIMIT 1").n[0])
    partes, off = [], 0
    while off < total:
        pg = q(f"SELECT {sel} FROM reporte_61 WHERE {w} "
               f"ORDER BY folio, codigo, cantidad, precio LIMIT {PAGINA} OFFSET {off}")
        if pg.empty:
            break
        partes.append(pg)
        off += len(pg)
        time.sleep(PAUSA)
    df = _norm(pd.concat(partes, ignore_index=True)) if partes else pd.DataFrame(columns=COLS)
    if len(df) != total:
        raise RuntimeError(f"{fecha}: paginación devolvió {len(df)} de {total}")
    return df


def ventas_dia_aurora(fecha):
    """Suplente: mismo día directo de Aurora (requiere VPN)."""
    from db import query
    sel = ", ".join(COLS)
    _, filas = query(
        f"SELECT /*+ MAX_EXECUTION_TIME(120000) */ {sel} FROM reportes.reporte_61 "
        f"WHERE fecha >= %s AND fecha < %s + INTERVAL 1 DAY "
        f"AND estatus='Activa' AND precio>0 AND cantidad>0", (fecha, fecha))
    return _norm(pd.DataFrame(filas, columns=COLS))


def extraer_mes(a, m):
    """Un mes a parquet — mismo skip incremental que extract.py."""
    ruta = os.path.join(DATA, f"ventas_{a}_{m:02d}.parquet")
    ini = date(a, m, 1)
    fin_mes = (pd.Timestamp(ini) + pd.offsets.MonthEnd(0)).date()
    ayer = date.today() - timedelta(days=1)
    tope = min(fin_mes, ayer)
    if os.path.exists(ruta):
        prev = pd.read_parquet(ruta, columns=["fecha"])
        if not prev.empty and pd.to_datetime(prev.fecha).max().date() >= tope:
            print(f"  {a}-{m:02d}: ya existe completo (skip)", flush=True)
            return 0
        print(f"  {a}-{m:02d}: existe PARCIAL — re-extrayendo", flush=True)
    partes, fuentes = [], {"api": 0, "aurora": 0}
    d = ini
    while d <= tope:
        try:
            df = ventas_dia_api(d.isoformat())
            fuentes["api"] += 1
        except Exception as e:
            print(f"    {d}: API falló ({str(e)[:50]}) → Aurora", flush=True)
            df = ventas_dia_aurora(d.isoformat())
            fuentes["aurora"] += 1
        partes.append(df)
        d += timedelta(days=1)
    mes = pd.concat(partes, ignore_index=True)
    mes.to_parquet(ruta, index=False)
    print(f"  {a}-{m:02d}: {len(mes):,} renglones | {mes.codigo.nunique():,} SKUs | "
          f"fuentes: {fuentes} -> {os.path.basename(ruta)}", flush=True)
    return len(mes)


def _vendibles():
    ruta = os.path.join(BASE, "data", "cat_almacen_vendible.json")
    try:  # refresco oportunista si hay VPN
        from db import query
        _, filas = query("SELECT id_almacen, nombre FROM `2015epcom`.`cat_almacen` "
                         "WHERE vendible=1")
        alm = {str(r[0]): str(r[1]) for r in filas}
        json.dump(alm, open(ruta, "w"), ensure_ascii=False, indent=1)
    except Exception:
        alm = json.load(open(ruta))
    return sorted(alm, key=int)


def existencias_snapshot_api(fecha, ids_venta):
    """Snapshot agregado por código de UNA fecha (paginado por código)."""
    en = ",".join(f"'{i}'" for i in ids_venta)
    sel = (f"codigo, SUM(CAST(existencia_total AS DECIMAL(18,4))) AS existencia, "
           f"SUM(CAST(existencia AS DECIMAL(18,4))) AS disponible, "
           f"SUM(CASE WHEN almacen IN ({en}) THEN CAST(existencia AS DECIMAL(18,4)) "
           f"ELSE 0 END) AS disp_venta, "
           f"MAX(CAST(precio_1 AS DECIMAL(18,6))) AS p1, "
           f"MAX(CAST(precio_3 AS DECIMAL(18,6))) AS p3, "
           f"MAX(CAST(cantidad_bo AS DECIMAL(18,4))) AS backorder, "
           f"MAX(CAST(costo_prov AS DECIMAL(18,6))) AS costo_prov, "
           f"SUM(CAST(costo_total_dolares AS DECIMAL(18,4))) AS valor_stock")
    partes, off = [], 0
    while True:
        pg = q(f"SELECT {sel} FROM valor_inventario WHERE fecha = '{fecha}' "
               f"GROUP BY codigo ORDER BY codigo LIMIT {PAGINA} OFFSET {off}")
        if pg.empty:
            break
        partes.append(pg)
        if len(pg) < PAGINA:
            break
        off += len(pg)
        time.sleep(PAUSA)
    if not partes:
        return None
    df = pd.concat(partes, ignore_index=True)
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["codigo"] = (df.codigo.astype(str).str.replace("​", "", regex=False)
                    .str.replace("â€‹", "", regex=False).str.strip())
    return df


def existencias():
    """Completa los lunes faltantes de existencias_sem.parquet vía API
    (fallback Aurora por semana si la API falla)."""
    ruta = os.path.join(DATA, "existencias_sem.parquet")
    ex = pd.read_parquet(ruta)
    hechas = set(pd.to_datetime(ex.semana).dt.normalize())
    ids_venta = _vendibles()
    d = (pd.Timestamp.today().normalize() - pd.offsets.Week(weekday=0)).normalize()
    nuevas = []
    while d not in hechas and d > pd.Timestamp("2024-01-01"):
        snap = None
        for intento in range(3):  # lunes, martes, miércoles
            f = (d + pd.Timedelta(days=intento)).date().isoformat()
            try:
                snap = existencias_snapshot_api(f, ids_venta)
            except Exception as e:
                print(f"  {f}: API falló ({str(e)[:50]}) — se intenta Aurora al final", flush=True)
                snap = None
            if snap is not None and len(snap):
                break
        if snap is None or not len(snap):
            print(f"  semana {d.date()}: sin snapshot por API — usar extract.py existencias "
                  f"(Aurora) para esta semana", flush=True)
            break
        snap["semana"] = d
        nuevas.append(snap)
        print(f"  semana {d.date()}: {len(snap):,} códigos (API)", flush=True)
        d -= pd.Timedelta(weeks=1)
    if nuevas:
        out = pd.concat([ex] + nuevas, ignore_index=True)
        out.to_parquet(ruta, index=False)
        print(f"existencias_sem: +{sum(len(n) for n in nuevas):,} filas "
              f"({len(nuevas)} semana(s) nueva(s))", flush=True)
    else:
        print("existencias_sem: al día", flush=True)


def proveedores_inventario():
    """CENSO codigo→proveedor desde valor_inventario (usuario 2026-07-31: el
    77% de los DORMIDOS quedaba sin proveedor porque el censo de ventas solo
    mira 45 días — y un dormido no vende). El inventario trae proveedor para
    TODO lo que tiene stock, que es justo la población accionable. Salida:
    data/reporte61/proveedores_inventario.parquet (complemento del censo de
    ventas, que sigue mandando para los activos)."""
    import html as _html
    hoy = pd.Timestamp.today().normalize()
    try:
        fecha = None
        for d in range(1, 7):
            f = (hoy - pd.Timedelta(days=d)).date().isoformat()
            if len(q(f"SELECT codigo FROM valor_inventario WHERE fecha = '{f}' LIMIT 1",
                     reintentos=1)):
                fecha = f
                break
        if fecha is None:
            raise RuntimeError("sin snapshot de inventario reciente en la API")
        partes, off = [], 0
        while True:
            pg = q(f"SELECT codigo, MAX(proveedor) AS proveedor, MAX(remate) AS remate, "
                   f"MAX(clasificacion) AS clasificacion FROM valor_inventario "
                   f"WHERE fecha = '{fecha}' AND proveedor IS NOT NULL "
                   f"GROUP BY codigo ORDER BY codigo LIMIT {PAGINA} OFFSET {off}")
            if pg.empty:
                break
            partes.append(pg)
            if len(pg) < PAGINA:
                break
            off += len(pg)
            time.sleep(PAUSA)
        df = pd.concat(partes, ignore_index=True)
    except Exception as e:
        print(f"  API falló ({str(e)[:60]}) → plan B: Aurora vía VPN", flush=True)
        from db import query
        fecha = (hoy - pd.Timedelta(days=1)).date().isoformat()
        _, filas = query(
            "SELECT /*+ MAX_EXECUTION_TIME(180000) */ codigo, MAX(proveedor), "
            "MAX(remate), MAX(clasificacion) "
            "FROM `reportes`.`valor_inventario` WHERE fecha=%s "
            "GROUP BY codigo", (fecha,))
        df = pd.DataFrame(filas, columns=["codigo", "proveedor", "remate", "clasificacion"])
    df["codigo"] = (df.codigo.astype(str).str.replace("​", "", regex=False)
                    .str.replace("â€‹", "", regex=False).str.strip())
    # SIEMPRE html.unescape en textos de la BD (regla 2026-07-27, caso Hangzhou)
    df["proveedor"] = df.proveedor.astype(str).map(_html.unescape).str.strip()
    df["remate"] = df.remate.fillna("N").astype(str).str.strip()
    df["clasificacion"] = df.clasificacion.fillna("").astype(str).str.strip()
    df = df.drop_duplicates("codigo")
    df["fecha_censo"] = fecha  # frescura: escenarios/dormidos avisan si el censo envejece
    ruta = os.path.join(DATA, "proveedores_inventario.parquet")
    df.to_parquet(ruta, index=False)
    print(f"censo proveedor desde inventario ({fecha}): {len(df):,} códigos → {ruta}",
          flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "existencias":
        existencias()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "proveedores":
        proveedores_inventario()
        return
    hoy = date.today()
    meses = pd.period_range(pd.Timestamp(hoy) - pd.DateOffset(months=2), hoy, freq="M")
    print(f"Extrayendo {len(meses)} mes(es) vía API (Aurora de suplente)", flush=True)
    total = 0
    for p in meses:
        total += extraer_mes(p.year, p.month)
    print(f"EXTRACCION COMPLETA: {total:,} renglones nuevos", flush=True)


if __name__ == "__main__":
    main()
