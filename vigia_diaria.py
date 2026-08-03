# -*- coding: utf-8 -*-
"""VIGÍA DIARIA de stock / BO / costos / precios — vía API de BI (sin VPN).

Pedido del usuario (2026-07-30): "lo que sí se necesita validar, quizá no en
vivo pero sí todos los días, es el stock, el BO, los costos, si subieron o
bajaron, y lo mismo de los precios".

Es además el PASO 1 de la migración Aurora→API: el snapshot de inventario ya
validó EXACTO contra Aurora (2026-07-30, al centavo). El pipeline pesado sigue
usando el extracto semanal congelado; esto es la capa de vigilancia diaria.

Qué hace, cada día (encadenado en seguimiento_frenos.py, el cron diario):
  1. Baja el snapshot más reciente de `valor_inventario` para los códigos del
     motor: existencia total, cantidad_bo, costo_prov, precio_1, precio_3
     (agregado por código sobre todos los almacenes; consultas por lotes de
     250 códigos por el límite de 1000 filas / 30 req/min).
  2. Lo guarda INMUTABLE en data/vigia/snap_YYYY-MM-DD.parquet.
  3. Compara contra el snapshot anterior y reporta MOVIMIENTOS:
       · costo_prov subió/bajó ≥2% (umbral de la defensa de margen)
       · precio_1 / precio_3 cambió (son administrados: cualquier cambio cuenta)
       · stockout nuevo (tenía stock → 0) y reabastecido (0 → stock)
       · BO nuevo o que crece
     → out/vigia_cambios.csv + notificación macOS si hay algo relevante.

Uso:  ./.venv/bin/python vigia_diaria.py            # snapshot de hoy + comparación
      ./.venv/bin/python vigia_diaria.py FECHA      # snapshot de una fecha específica
"""
import glob
import os
import subprocess
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_bi import q  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, "data", "vigia")
LOTE = 250
PAUSA = 2.1
UMBRAL_COSTO = 0.02    # ±2%: mismo umbral que la defensa de margen
UMBRAL_PRECIO = 0.001  # listas administradas: cualquier cambio real (>0.1%)


def _codigos_motor():
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    return sorted(recos.codigo.astype(str).unique())


def _ultima_fecha():
    """Sondea la fecha de snapshot más reciente SIN un MAX() sobre toda la
    tabla (72M+ filas ⇒ el servidor puede dar 500 por timeout bajo carga)."""
    hoy = pd.Timestamp.today().normalize()
    for d in range(6):
        f = (hoy - pd.Timedelta(days=d)).date().isoformat()
        r = q(f"SELECT codigo FROM valor_inventario WHERE fecha = '{f}' LIMIT 1")
        if len(r):
            return f
    raise RuntimeError("sin snapshot de inventario en los últimos 6 días")


def _snapshot_aurora(fecha):
    """PLAN B (2026-07-30, Redshift caído): mismo snapshot pero directo de
    Aurora vía VPN — una sola consulta agregada; se filtra a códigos del motor
    en pandas. Misma salida que la ruta API."""
    from db import query
    _, filas = query(
        "SELECT /*+ MAX_EXECUTION_TIME(120000) */ codigo, SUM(existencia), "
        "MAX(cantidad_bo), MAX(costo_prov), MAX(precio_1), MAX(precio_3) "
        "FROM `reportes`.`valor_inventario` WHERE fecha=%s GROUP BY codigo",
        (fecha,))
    df = pd.DataFrame(filas, columns=["codigo", "existencia", "bo", "costo_prov",
                                      "precio_1", "precio_3"])
    if df.empty:
        return df
    codigos = set(_codigos_motor())
    return df[df.codigo.isin(codigos)].reset_index(drop=True)


def snapshot(fecha=None):
    """Baja el snapshot de valor_inventario (fecha dada o la más reciente).
    Fuente primaria: API (sin VPN); si Redshift está caído, cae a Aurora."""
    os.makedirs(DIR, exist_ok=True)
    if fecha is None:
        try:
            fecha = _ultima_fecha()
        except Exception:
            # API caída: ayer, que en Aurora siempre existe
            fecha = (pd.Timestamp.today().normalize() - pd.Timedelta(days=1)).date().isoformat()
    ruta = os.path.join(DIR, f"snap_{fecha}.parquet")
    if os.path.exists(ruta):
        print(f"snapshot {fecha}: ya existe ({ruta})", flush=True)
        return ruta
    t0 = time.time()
    fuente = "api"
    try:
        codigos = _codigos_motor()
        partes = []
        for i in range(0, len(codigos), LOTE):
            sub = codigos[i:i + LOTE]
            lista = ",".join("'" + c.replace("'", "''") + "'" for c in sub)
            df = q(f"SELECT codigo, SUM(CAST(existencia AS DECIMAL(18,4))) AS existencia, "
                   f"MAX(CAST(cantidad_bo AS DECIMAL(18,4))) AS bo, "  # MAX: viene repetido por almacén
                   f"MAX(CAST(costo_prov AS DECIMAL(18,6))) AS costo_prov, "
                   f"MAX(CAST(precio_1 AS DECIMAL(18,6))) AS precio_1, "
                   f"MAX(CAST(precio_3 AS DECIMAL(18,6))) AS precio_3 "
                   f"FROM valor_inventario WHERE fecha = '{fecha}' "
                   f"AND codigo IN ({lista}) GROUP BY codigo LIMIT 1000")
            if len(df):
                partes.append(df)
            time.sleep(PAUSA)
        snap = pd.concat(partes, ignore_index=True)
    except Exception as e:
        print(f"  API caída ({str(e)[:60]}) → plan B: Aurora vía VPN", flush=True)
        snap = _snapshot_aurora(fecha)
        fuente = "aurora"
    if snap.empty:
        raise RuntimeError(f"sin datos de inventario para {fecha} en ninguna fuente")
    for c in ["existencia", "bo", "costo_prov", "precio_1", "precio_3"]:
        snap[c] = pd.to_numeric(snap[c], errors="coerce")
    snap["fecha"] = fecha
    snap["fuente"] = fuente
    snap.to_parquet(ruta, index=False)
    print(f"snapshot {fecha}: {len(snap):,} códigos en {time.time()-t0:.0f}s "
          f"(fuente: {fuente}) → {ruta}", flush=True)
    return ruta


def _registrar_revision_costos(df):
    """DISPARADOR DE REVISIÓN DE PRECIOS por movimiento de costo (usuario
    2026-07-30): cada 'COSTO SUBIÓ/BAJÓ' del día entra a un registro
    PERSISTENTE (out/revision_costos.csv) que alimenta los chips y el filtro
    '💲 Costo movió' del reporte. Si el mismo código vuelve a moverse, el
    evento se ACTUALIZA acumulando contra el costo base del primer evento
    pendiente (no se duplica). Se estima el margen nuevo al precio actual:
    margen' = 1 − (costo_hoy/costo_antes)·(1−margen) — mismo neto, costo nuevo."""
    cos = df[df.movimiento.str.startswith("COSTO")].copy()
    if cos.empty:
        return
    ruta = os.path.join(BASE, "out", "revision_costos.csv")
    reg = (pd.read_csv(ruta) if os.path.exists(ruta) else
           pd.DataFrame(columns=["codigo", "fecha_detectado", "fecha_ultimo",
                                 "costo_base", "costo_hoy", "pct",
                                 "margen_antes", "margen_nuevo"]))
    try:
        recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
        marg = recos.set_index("codigo").margen_actual
    except Exception:
        marg = pd.Series(dtype=float)
    hoy = cos.a.iloc[0]
    for _, r in cos.iterrows():
        m0 = float(marg.get(r.codigo, float("nan")))
        vivo = False
        if r.codigo in set(reg.codigo):
            i = reg.index[reg.codigo == r.codigo][0]
            # EXPIRACIÓN (auditoría 2026-07-31, N2): un evento con >21 días sin
            # moverse ya fue absorbido por el ciclo — el movimiento nuevo abre
            # EVENTO NUEVO (base y margen frescos); si no, el pct acumularía
            # contra un costo de otra época y el margen se descontaría DOS veces
            vivo = (pd.Timestamp(hoy) - pd.Timestamp(reg.at[i, "fecha_ultimo"])).days <= 21
        if vivo:
            base = float(reg.at[i, "costo_base"])
            reg.at[i, "costo_hoy"] = r.hoy
            reg.at[i, "pct"] = round(100 * (r.hoy / base - 1), 1)
            reg.at[i, "fecha_ultimo"] = hoy
            if pd.notna(m0):
                reg.at[i, "margen_nuevo"] = round(1 - (r.hoy / base) * (1 - m0), 4)
        else:
            reg = reg[reg.codigo != r.codigo].reset_index(drop=True)
            m1 = round(1 - (r.hoy / r.antes) * (1 - m0), 4) if pd.notna(m0) else float("nan")
            reg.loc[len(reg)] = [r.codigo, hoy, hoy, r.antes, r.hoy,
                                 round(100 * (r.hoy / r.antes - 1), 1), m0, m1]
    # purga de filas expiradas sin movimiento nuevo (solo estorban el conteo)
    reg = reg[(pd.Timestamp(hoy) - pd.to_datetime(reg.fecha_ultimo)).dt.days <= 45]
    reg.to_csv(ruta, index=False)
    print(f"  revisión de precios por costo: {len(cos)} evento(s) del día → "
          f"{ruta} ({len(reg)} pendientes)", flush=True)


def comparar():
    """Diff de los dos snapshots más recientes → movimientos del día."""
    rutas = sorted(glob.glob(os.path.join(DIR, "snap_*.parquet")))
    if len(rutas) < 2:
        print("comparar: aún no hay dos snapshots (mañana ya habrá diff)", flush=True)
        return None
    ayer, hoy = pd.read_parquet(rutas[-2]), pd.read_parquet(rutas[-1])
    f_ayer, f_hoy = ayer.fecha.iloc[0], hoy.fecha.iloc[0]
    m = ayer.merge(hoy, on="codigo", suffixes=("_ant", "_hoy"))

    def _pct(a, h):
        return (h - a) / a.where(a != 0)

    cambios = []
    d_costo = _pct(m.costo_prov_ant, m.costo_prov_hoy)
    for signo, mask in [("SUBIÓ", d_costo >= UMBRAL_COSTO), ("BAJÓ", d_costo <= -UMBRAL_COSTO)]:
        for _, r in m[mask.fillna(False)].iterrows():
            cambios.append((r.codigo, f"COSTO {signo}", r.costo_prov_ant, r.costo_prov_hoy,
                            round(100 * (r.costo_prov_hoy / r.costo_prov_ant - 1), 1)))
    for lista in ["precio_1", "precio_3"]:
        d = _pct(m[f"{lista}_ant"], m[f"{lista}_hoy"])
        for signo, mask in [("SUBIÓ", d >= UMBRAL_PRECIO), ("BAJÓ", d <= -UMBRAL_PRECIO)]:
            for _, r in m[mask.fillna(False)].iterrows():
                cambios.append((r.codigo, f"{lista.upper()} {signo}", r[f"{lista}_ant"],
                                r[f"{lista}_hoy"],
                                round(100 * (r[f"{lista}_hoy"] / r[f"{lista}_ant"] - 1), 1)))
    so = m[(m.existencia_ant > 0) & (m.existencia_hoy <= 0)]
    re = m[(m.existencia_ant <= 0) & (m.existencia_hoy > 0)]
    bo = m[(m.bo_hoy > m.bo_ant) & (m.bo_hoy > 0)]
    for _, r in so.iterrows():
        cambios.append((r.codigo, "STOCKOUT", r.existencia_ant, 0, None))
    for _, r in re.iterrows():
        cambios.append((r.codigo, "REABASTECIDO", 0, r.existencia_hoy, None))
    for _, r in bo.iterrows():
        cambios.append((r.codigo, "BO CRECIÓ", r.bo_ant, r.bo_hoy, None))

    df = pd.DataFrame(cambios, columns=["codigo", "movimiento", "antes", "hoy", "pct"])
    df.insert(0, "de", f_ayer); df.insert(1, "a", f_hoy)
    ruta_out = os.path.join(BASE, "out", "vigia_cambios.csv")
    df.to_csv(ruta_out, index=False)
    _registrar_revision_costos(df)
    resumen = df.movimiento.value_counts().to_dict()
    print(f"movimientos {f_ayer} → {f_hoy}: {resumen if resumen else 'sin cambios relevantes'}"
          f" → {ruta_out}", flush=True)

    n_costo = sum(v for k, v in resumen.items() if k.startswith("COSTO"))
    n_precio = sum(v for k, v in resumen.items() if k.startswith("PRECIO"))
    if n_costo or n_precio or len(so):
        msg = (f"Vigía diaria: {n_costo} costos, {n_precio} precios de lista, "
               f"{len(so)} stockouts, {len(re)} reabastecidos, {len(bo)} BO al alza")
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg}" with title "Motor de Precio v3"'],
                           check=False, timeout=10)
        except Exception:
            pass
    return df


def correr(fecha=None):
    snapshot(fecha)
    return comparar()


if __name__ == "__main__":
    correr(sys.argv[1] if len(sys.argv) > 1 else None)
