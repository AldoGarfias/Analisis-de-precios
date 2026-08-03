# -*- coding: utf-8 -*-
"""Extrae ventas desde `reportes.reporte_61` (única fuente en esta fase) a parquet.

Solo LECTURA contra Aurora (reusa db.query). `reporte_61` es una tabla de hechos
denormalizada (~72M renglones, ventas a nivel línea) con precio realizado, costo,
tipo de cambio, canal (`via`), tipo de precio y descuento ya unidos.

Decisiones de esta fase (según instrucciones del usuario):
  - Fuente ÚNICA: reporte_61. No se usan marca ni linea para nada.
  - Identidad del SKU: `codigo`.
  - Demanda = ventas facturadas: estatus='Activa', precio>0, cantidad>0.
    (Se excluyen 'Cotizacion', 'Nota credito' y 'Cancelada'.)
  - Lectura POR DÍA (la tabla es lenta: un mes de un jalón pasa de 180s en el
    server). Cada día usa el índice de `fecha` de forma selectiva y regresa en
    segundos; se acumulan en un parquet por mes.

Salida: data/reporte61/ventas_YYYY_MM.parquet  (+ un índice al terminar).

Uso:
  ./.venv/bin/python extract.py                 # últimos 3 meses (default)
  ./.venv/bin/python extract.py 2025-07 2026-07 # rango [ini, fin) por mes

NOTA de moneda (hallazgo #6 de REVISION_V2): `precio` y `costo_venta` parecen estar
en la MISMA moneda (USD de importación); `tipo_cambio` viene por renglón para pasar
a MXN. Aquí se extrae CRUDO, sin convertir. La conversión y el assert de moneda se
hacen en el panel, nunca mezclando escalas en silencio.
"""
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import conectar_lectura, query  # noqa: E402

# La VPN puede parpadear en una corrida larga; reintentar con reconexión.
REINTENTOS = 4
ESPERA_S = 6


class _Con:
    """Sostiene la conexión y la reabre si se cae (VPN intermitente)."""

    def __init__(self):
        self.con = None

    def get(self):
        if self.con is None:
            self.con = conectar_lectura()
        return self.con

    def reset(self):
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = None

    def close(self):
        self.reset()


def _query_reintentos(holder, sql, params, etiqueta=""):
    for intento in range(1, REINTENTOS + 1):
        try:
            return query(sql, params, con=holder.get())
        except pymysql.err.OperationalError as e:
            if intento == REINTENTOS:
                raise
            print(f"    reintento {intento}/{REINTENTOS} {etiqueta}: {str(e)[:70]}",
                  flush=True)
            holder.reset()
            time.sleep(ESPERA_S)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "reporte61")
TABLA = "`reportes`.`reporte_61`"

# Columnas que sí sirven al motor (sin marca/linea):
#   - palanca de elasticidad: precio (lista 1/3 administrada), tipo_precio
#   - demanda: fecha, codigo, cantidad
#   - precio realizado neto y margen: subtotal (= neto), costo_venta, tipo_cambio
#   - stack de descuentos B2B: descuento_uno (≈20, special si ≠20), descuento
#     (por cliente/clasificacion_descuento), desc_fin (forma de pago)
#   - segmentación cliente: clasificacion_descuento, concepto (identifica proyectos)
#   - dedup semanal cliente-SKU (hallazgo v1): codigo_cliente, folio
#   - pesos v1: via (canal)
COLS = [
    "fecha", "folio", "codigo", "codigo_cliente",
    "cantidad", "precio", "tipo_precio",
    "descuento_uno", "descuento", "desc_fin", "subtotal",
    "costo_venta", "tipo_cambio",
    "concepto", "clasificacion_descuento", "via",
    "kit",  # 'Si' = modelo virtual que agrupa componentes (se excluyen del panel)
]

# Guard de tiempo por consulta (ms). Por DÍA la consulta es chica (~seg).
MAX_MS = 60000


def _meses(ini, fin):
    """Lista de (anio, mes) desde ini (incl.) hasta fin (excl.), ambos 'YYYY-MM'."""
    ai, mi = int(ini[:4]), int(ini[5:7])
    af, mf = int(fin[:4]), int(fin[5:7])
    out = []
    a, m = ai, mi
    while (a, m) < (af, mf):
        out.append((a, m))
        m += 1
        if m == 13:
            a, m = a + 1, 1
    return out


def _rango_mes(a, m):
    ini = date(a, m, 1)
    fin = date(a + 1, 1, 1) if m == 12 else date(a, m + 1, 1)
    return ini.isoformat(), fin.isoformat()


_SEL = ",".join("`" + c + "`" for c in COLS)
_SQL_DIA = (
    f"SELECT /*+ MAX_EXECUTION_TIME({MAX_MS}) */ {_SEL} FROM {TABLA} "
    "WHERE fecha >= %s AND fecha < %s "
    "AND estatus = 'Activa' AND precio > 0 AND cantidad > 0"
)


def extraer_mes(holder, a, m):
    """Extrae un mes (día por día) de ventas Activas a un parquet. Devuelve # renglones."""
    ruta = os.path.join(DATA, f"ventas_{a}_{m:02d}.parquet")
    if os.path.exists(ruta):
        # skip SOLO si el archivo ya cubre el mes hasta donde puede: el fin del
        # mes, o ayer si el mes sigue corriendo (los datos llegan al día previo).
        # Un mes parcial (p.ej. el actual) se RE-extrae completo.
        prev = pd.read_parquet(ruta, columns=["fecha"])
        tope = min(date.fromisoformat(_rango_mes(a, m)[1]) - timedelta(days=1),
                   date.today() - timedelta(days=1))
        cubre = (len(prev) and pd.Timestamp(prev.fecha.max()).date() >= tope)
        if cubre:
            print(f"  {a}-{m:02d}: ya existe completo (skip)", flush=True)
            return -1
        print(f"  {a}-{m:02d}: existe PARCIAL (hasta "
              f"{pd.Timestamp(prev.fecha.max()).date() if len(prev) else '—'}) — re-extrayendo",
              flush=True)
    ini, fin = _rango_mes(a, m)
    d = date.fromisoformat(ini)
    dfin = date.fromisoformat(fin)
    partes = []
    t0 = time.time()
    while d < dfin:
        sig = d + timedelta(days=1)
        cols, filas = _query_reintentos(holder, _SQL_DIA, (d.isoformat(), sig.isoformat()),
                                         etiqueta=d.isoformat())
        if filas:
            partes.append(pd.DataFrame(filas, columns=cols))
        d = sig
    df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=COLS)
    os.makedirs(DATA, exist_ok=True)
    df.to_parquet(ruta, index=False)
    n_sku = df["codigo"].nunique() if len(df) else 0
    print(f"  {a}-{m:02d}: {len(df):>8} renglones | {n_sku:>6} SKUs | "
          f"{time.time()-t0:.0f}s -> {os.path.basename(ruta)}", flush=True)
    return len(df)


def extraer(ini, fin):
    holder = _Con()
    total = 0
    try:
        meses = _meses(ini, fin)
        print(f"Extrayendo {len(meses)} mes(es) de {ini} a {fin} (excl.) desde reporte_61",
              flush=True)
        for a, m in meses:
            n = extraer_mes(holder, a, m)
            total += max(n, 0)
    finally:
        holder.close()
    print(f"EXTRACCION COMPLETA: {total} renglones en data/reporte61/", flush=True)
    return total


def extraer_existencias(ini, fin):
    """Snapshot SEMANAL por SKU desde reportes.valor_inventario (diaria, por
    almacén, desde 2018-01). Se agrega EN EL SERVIDOR (~5-10s por semana):
      - existencia   = SUM(existencia_total): físico total (incluye apartada)
      - disponible   = SUM(existencia): SIN la apartada (no comprometida)
      - disp_venta   = SUM(existencia) SOLO en almacenes de VENTA (vendible=1
                       en cat_almacen; ~128 físicos de los ~820 registros —
                       el resto son virtuales/cliente/consigna). ESTE es el
                       stock para meses de inventario (regla del negocio).
      - p1, p3       = precio de lista administrado vigente ese día (validado
                       contra la lista reconstruida de transacciones)
      - backorder    = MAX(cantidad_bo): mercancía en REPOSICIÓN (en camino,
                       libre para venta), dimensionada por ventas mensuales.
                       Viene REPETIDO por almacén; SUM lo inflaría ×~820.
      - costo_prov   = MAX(costo_prov): costo de REPOSICIÓN (lo que costaría
                       comprar HOY al proveedor).
      - valor_stock  = SUM(costo_total_dolares): valor del stock en mano →
                       costo PROMEDIO del stock = valor_stock / existencia.
                       La comparativa costo_prov vs costo_promedio detecta
                       "incrementos de laboratorio": el proveedor subió pero
                       NO compramos, así que el stock en mano costó barato.
    Se toma el lunes de cada semana (mismo inicio que el panel); si ese día no
    tiene corte, se intenta martes/miércoles.

    Salida: data/reporte61/existencias_sem.parquet
    """
    holder = _Con()
    ruta = os.path.join(DATA, "existencias_sem.parquet")
    # RESUME: si hay un parquet previo (aunque sea parcial), conservar sus
    # semanas y extraer solo las faltantes. CHECKPOINT: se guarda cada 10
    # semanas para que un corte no pierda horas de trabajo.
    partes = []
    hechas = set()
    if os.path.exists(ruta):
        prev = pd.read_parquet(ruta)
        if "costo_prov" in prev.columns:  # solo si tiene el esquema vigente
            partes.append(prev)
            hechas = set(pd.to_datetime(prev.semana.unique()))
            print(f"  resume: {len(hechas)} semanas ya extraídas", flush=True)
    d = date.fromisoformat(ini + "-01") if len(ini) == 7 else date.fromisoformat(ini)
    dfin = date.fromisoformat(fin + "-01") if len(fin) == 7 else date.fromisoformat(fin)
    d = d - timedelta(days=d.weekday())  # alinear al lunes
    t0 = time.time()
    n_sem = 0

    def _guardar():
        out = pd.concat(partes, ignore_index=True)
        for c in ["existencia", "disponible", "disp_venta", "p1", "p3", "backorder",
                  "costo_prov", "valor_stock"]:
            out[c] = out[c].astype(float)
        out.to_parquet(ruta, index=False)
        return out
    # IDs de almacenes de VENTA (vendible=1) una sola vez; IN-list evita el
    # JOIN por semana (35s -> ~8s).
    _, filas_alm = _query_reintentos(
        holder, "SELECT id_almacen FROM `2015epcom`.`cat_almacen` WHERE vendible=1",
        None, etiqueta="cat_almacen")
    ids_venta = ",".join(str(int(r[0])) for r in filas_alm)
    print(f"  almacenes de venta (vendible=1): {len(filas_alm)}", flush=True)
    try:
        while d < dfin:
            if pd.Timestamp(d) in hechas:
                d += timedelta(days=7)
                continue
            filas = []
            for intento in range(3):  # lunes, martes, miércoles
                dia = d + timedelta(days=intento)
                _, filas = _query_reintentos(
                    holder,
                    f"SELECT /*+ MAX_EXECUTION_TIME({MAX_MS}) */ codigo, "
                    "SUM(existencia_total), SUM(existencia), "
                    f"SUM(CASE WHEN almacen IN ({ids_venta}) THEN existencia ELSE 0 END), "
                    "MAX(precio_1), MAX(precio_3), MAX(cantidad_bo), "
                    "MAX(costo_prov), SUM(costo_total_dolares) "
                    "FROM `reportes`.`valor_inventario` "
                    "WHERE fecha=%s GROUP BY codigo",
                    (dia.isoformat(),), etiqueta=f"exist {dia}")
                if filas:
                    break
            if filas:
                df = pd.DataFrame(filas, columns=["codigo", "existencia", "disponible",
                                                  "disp_venta", "p1", "p3", "backorder",
                                                  "costo_prov", "valor_stock"])
                df["semana"] = pd.Timestamp(d)
                partes.append(df)
                n_sem += 1
            else:
                print(f"  {d}: SIN corte de inventario (semana omitida)", flush=True)
            if n_sem and n_sem % 10 == 0:
                os.makedirs(DATA, exist_ok=True)
                _guardar()  # checkpoint
                print(f"  ... {n_sem} semanas nuevas ({time.time()-t0:.0f}s) [checkpoint]",
                      flush=True)
            d += timedelta(days=7)
    finally:
        holder.close()
    os.makedirs(DATA, exist_ok=True)
    out = _guardar()
    print(f"existencias_sem.parquet: {len(out):,} filas | {out.semana.nunique()} semanas "
          f"({n_sem} nuevas) | {out.codigo.nunique():,} SKUs | {time.time()-t0:.0f}s", flush=True)


def extraer_kits(ini, fin):
    """Censo de códigos KIT ('Si' en reporte_61) del rango, por día (ligero).

    Los parquets de ventas ya extraídos no traen la columna kit (se agregó
    después); este censo permite excluirlos del panel sin re-extraer 24 meses.
    Salida: data/reporte61/kits.parquet (codigo únicos).
    """
    holder = _Con()
    kits = set()
    d = date.fromisoformat(ini + "-01") if len(ini) == 7 else date.fromisoformat(ini)
    dfin = date.fromisoformat(fin + "-01") if len(fin) == 7 else date.fromisoformat(fin)
    t0 = time.time()
    n_dias = 0
    try:
        while d < dfin:
            sig = d + timedelta(days=1)
            _, filas = _query_reintentos(
                holder,
                f"SELECT /*+ MAX_EXECUTION_TIME({MAX_MS}) */ DISTINCT codigo FROM {TABLA} "
                "WHERE fecha >= %s AND fecha < %s AND kit='Si'",
                (d.isoformat(), sig.isoformat()), etiqueta=f"kits {d}")
            kits.update(r[0] for r in filas)
            n_dias += 1
            if n_dias % 60 == 0:
                print(f"  ... {n_dias} días, {len(kits)} kits ({time.time()-t0:.0f}s)", flush=True)
            d = sig
    finally:
        holder.close()
    df = pd.DataFrame({"codigo": sorted(kits)})
    os.makedirs(DATA, exist_ok=True)
    ruta = os.path.join(DATA, "kits.parquet")
    df.to_parquet(ruta, index=False)
    print(f"kits.parquet: {len(df)} códigos kit en {n_dias} días | {time.time()-t0:.0f}s", flush=True)


def extraer_proveedores(dias=45):
    """Censo codigo→proveedor desde reporte_61 (últimos N días, por día).

    El proveedor es un atributo del SKU (estable); no amerita re-extraer 24
    meses. Se toma el proveedor MODAL de las ventas recientes por código.
    Salida: data/reporte61/proveedores.parquet (codigo, proveedor).
    """
    holder = _Con()
    pares = {}
    d = date.today() - timedelta(days=dias)
    t0 = time.time()
    try:
        while d < date.today():
            sig = d + timedelta(days=1)
            _, filas = _query_reintentos(
                holder,
                f"SELECT /*+ MAX_EXECUTION_TIME({MAX_MS}) */ codigo, proveedor, COUNT(*) "
                f"FROM {TABLA} WHERE fecha >= %s AND fecha < %s AND estatus='Activa' "
                "GROUP BY codigo, proveedor",
                (d.isoformat(), sig.isoformat()), etiqueta=f"prov {d}")
            for cod, prov, n in filas:
                if prov:
                    pares[(cod, prov)] = pares.get((cod, prov), 0) + n
            d = sig
    finally:
        holder.close()
    df = pd.DataFrame([(c, p, n) for (c, p), n in pares.items()],
                      columns=["codigo", "proveedor", "n"])
    modal = df.sort_values("n", ascending=False).drop_duplicates("codigo")[["codigo", "proveedor"]]
    # la BD guarda algunos nombres con entidades HTML crudas (&amp;, &Oacute;...):
    # normalizar aquí para que filtros/joins comparen el MISMO string siempre
    import html as _html
    modal["proveedor"] = modal.proveedor.map(lambda p: _html.unescape(str(p)))
    os.makedirs(DATA, exist_ok=True)
    ruta = os.path.join(DATA, "proveedores.parquet")
    modal.to_parquet(ruta, index=False)
    print(f"proveedores.parquet: {len(modal):,} códigos | "
          f"{modal.proveedor.nunique()} proveedores | {time.time()-t0:.0f}s", flush=True)


def _defaults():
    """Últimos 3 meses hasta el mes actual (según el reloj del sistema)."""
    hoy = date.today()
    fin = date(hoy.year + (hoy.month // 12), (hoy.month % 12) + 1, 1)  # inicio del prox mes
    # 3 meses atrás desde el mes actual
    a, m = hoy.year, hoy.month - 2
    while m <= 0:
        a, m = a - 1, m + 12
    return f"{a}-{m:02d}", f"{fin.year}-{fin.month:02d}"


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "existencias":
        ini, fin = (sys.argv[2], sys.argv[3]) if len(sys.argv) >= 4 else _defaults()
        extraer_existencias(ini, fin)
    elif len(sys.argv) >= 2 and sys.argv[1] == "kits":
        ini, fin = (sys.argv[2], sys.argv[3]) if len(sys.argv) >= 4 else _defaults()
        extraer_kits(ini, fin)
    elif len(sys.argv) >= 2 and sys.argv[1] == "proveedores":
        extraer_proveedores(int(sys.argv[2]) if len(sys.argv) >= 3 else 45)
    else:
        if len(sys.argv) >= 3:
            ini, fin = sys.argv[1], sys.argv[2]
        else:
            ini, fin = _defaults()
        extraer(ini, fin)
