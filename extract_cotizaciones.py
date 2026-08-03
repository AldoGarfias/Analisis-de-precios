# -*- coding: utf-8 -*-
"""ETAPA 2 — extracción del EMBUDO WIN-RATE: renglones con estatus='Cotizacion'
de reportes.reporte_61 (2024→hoy), aprobada por el usuario 2026-08-01
("Aplica las 4", punto 4: el modelo bid-response del v1 sobre la BD nueva).

Por qué: el estudio de canal (docs/ANALISIS_CANAL_LINEA.md) mostró que parte
de la elasticidad del canal vendedor es sobre-descuento preventivo del
vendedor, no demanda. El antídoto estructural (literatura de fuerza de ventas)
es darle al vendedor la tasa de éxito por nivel de precio — que sale de las
cotizaciones: P(ganar | precio relativo a la lista aplicable, contexto).

Lecciones del v1 que se conservan al modelar (NO en la extracción):
  peso por cotización (canal via + lista completa), dedup cliente-SKU-semana,
  rel_precio contra la lista APLICABLE (tipo_precio 1 vs 3).

Extrae de lo MÁS RECIENTE hacia atrás (los meses nuevos sirven antes).
Titular: API de BI; suplente: Aurora vía VPN, día por día (mismo patrón que
extract_api.py). Salida: data/reporte61/cotiz_YYYY_MM.parquet.

Uso:  ./.venv/bin/python extract_cotizaciones.py [YYYY-MM inicial hacia atrás]
"""
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "reporte61") if os.path.basename(BASE) == "data" \
    else os.path.join(BASE, "data", "reporte61")

COLS = ["fecha", "folio", "codigo", "codigo_cliente", "cantidad", "precio",
        "tipo_precio", "descuento_uno", "descuento", "desc_fin", "subtotal",
        "costo_venta", "tipo_cambio", "concepto", "via", "kit"]
SEL = ", ".join(COLS)


def _dia_api(f):
    from api_bi import q
    return q(f"SELECT {SEL} FROM reporte_61 WHERE fecha='{f}' "
             f"AND estatus='Cotizacion' AND CAST(precio AS DECIMAL(18,4))>0 "
             f"AND CAST(cantidad AS DECIMAL(18,4))>0 LIMIT 500000")


def _dia_aurora(f):
    from db import query
    cols, filas = query(
        f"SELECT /*+ MAX_EXECUTION_TIME(120000) */ {SEL} "
        f"FROM `reportes`.`reporte_61` WHERE fecha=%s AND estatus='Cotizacion' "
        f"AND precio>0 AND cantidad>0", (f,))
    return pd.DataFrame(filas, columns=COLS)


def extraer_mes(a, m):
    ruta = os.path.join(DATA, f"cotiz_{a}_{m:02d}.parquet")
    ini = date(a, m, 1)
    fin = min((pd.Timestamp(ini) + pd.offsets.MonthEnd(0)).date(),
              date.today() - timedelta(days=1))
    if fin < ini:
        return 0
    if os.path.exists(ruta):
        prev = pd.read_parquet(ruta, columns=["fecha"])
        if not prev.empty and pd.to_datetime(prev.fecha).max().date() >= fin:
            print(f"  {a}-{m:02d}: completo (skip)", flush=True)
            return 0
    partes, d = [], ini
    while d <= fin:
        f = d.isoformat()
        df, fuente = None, None
        # al perder conexión: ESPERAR y reintentar (hasta 30 min), no saltar
        # meses en falso (lección 2026-08-01: una caída de VPN dejó 3 meses
        # huecos marcados "parcial" mientras el loop seguía corriendo)
        for intento in range(7):
            try:
                df = _dia_api(f)
                fuente = "api"
                break
            except Exception:
                pass
            try:
                df = _dia_aurora(f)
                fuente = "aurora"
                break
            except Exception as e:
                espera = min(300, 30 * (intento + 1))
                print(f"  {f}: sin conexión ({str(e)[:50]}) — reintento en "
                      f"{espera}s ({intento+1}/7)", flush=True)
                time.sleep(espera)
        if df is None:
            raise SystemExit(f"  {f}: sin conexión tras ~30 min de reintentos — "
                             f"DETENIDO (relanzar cuando vuelva la VPN; los meses "
                             f"completos se saltan solos)")
        if len(df):
            partes.append(df)
        d += timedelta(days=1)
        time.sleep(0.2)
    if not partes:
        return 0
    mes = pd.concat(partes, ignore_index=True)
    # misma higiene de migración: NULL→'', limpiar U+200B/mojibake en codigo
    for c in mes.columns:
        if mes[c].dtype == object:
            mes[c] = mes[c].fillna("")
    mes["codigo"] = (mes.codigo.astype(str).str.replace("​", "", regex=False)
                     .str.replace("â€‹", "", regex=False).str.strip())
    os.makedirs(DATA, exist_ok=True)
    mes.to_parquet(ruta, index=False)
    print(f"  {a}-{m:02d}: {len(mes):,} renglones ({fuente}) → {ruta}", flush=True)
    return len(mes)


def main():
    tope = sys.argv[1] if len(sys.argv) > 1 else None
    hoy = date.today()
    meses = pd.period_range("2024-01", hoy, freq="M")[::-1]  # reciente → atrás
    if tope:
        meses = [p for p in meses if str(p) <= tope]
    total = 0
    for p in meses:
        total += extraer_mes(p.year, p.month)
    print(f"COTIZACIONES: {total:,} renglones nuevos", flush=True)


if __name__ == "__main__":
    main()
