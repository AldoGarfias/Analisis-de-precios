# -*- coding: utf-8 -*-
"""REGISTRO E INTELIGENCIA DE COMPETENCIA (usuario 2026-08-07: "primero un
registro por cada competidor —una BD por competidor—, después identificar
cambios, ANTES de pasar a la comparativa con nuestros modelos").

Capa 1 — REGISTRO (`consolidar`): una BD por competidor en
  data/competencia/db/<fuente>.parquet — historia modelo×fecha con
  precio_lista, descuento_pct, precio_venta, moneda, existencia, marca.
  Se alimenta de los CSV crudos del feed (extract_competencia.py) por
  merge idempotente (dedup modelo+fecha, gana el más reciente).

Capa 2 — CAMBIOS (`cambios`): por competidor y modelo, comparando fechas
  consecutivas EN SU PROPIA MONEDA (jamás cruzar FX aquí — el tipo de cambio
  metería ruido de cambios falsos):
    PRECIO SUBIÓ/BAJÓ  |Δprecio_venta| ≥ 1%
    ALTA               modelo aparece por primera vez
    STOCKOUT           existencia >0 → 0
    REABASTECIDO       existencia 0 → >0
  Salida: data/competencia/cambios.parquet (historia completa) +
  out/competencia_cambios.csv (últimos 7 días, para revisión rápida).

Capa 3 — comparativa con nuestros modelos: PENDIENTE por decisión del
  usuario (se construye sobre estas dos capas).

Cron: corre a diario en la cadena de 8:30 tras extract_competencia.
Uso:  ./.venv/bin/python competencia.py [consolidar|cambios]   (default: ambos)
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
CRUDOS = os.path.join(BASE, "data", "competencia")
DB = os.path.join(CRUDOS, "db")
UMBRAL_PRECIO = 0.01   # ±1% en su propia moneda = cambio real

COLS = ["fecha", "modelo", "marca", "categoria", "precio_lista",
        "descuento_pct", "precio_venta", "moneda", "existencia"]


def _tc_por_semana():
    """Tipo de cambio por semana desde el panel (mediana), para convertir
    MXN→USD (usuario 2026-08-07). Fallback: mediana de las últimas 4 semanas."""
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"),
                          columns=["semana", "tc"])
    tcs = pan.groupby("semana").tc.median()
    reciente = float(pan[pan.semana >= pan.semana.max()
                         - pd.Timedelta(weeks=4)].tc.median())
    return tcs, reciente


def consolidar():
    """CSV crudos → una BD parquet por competidor (idempotente)."""
    os.makedirs(DB, exist_ok=True)
    rutas = [r for r in glob.glob(os.path.join(CRUDOS, "*.csv"))
             if not os.path.basename(r).startswith("_")]
    por_fuente = {}
    for r in rutas:
        fuente = os.path.basename(r).rsplit("_", 1)[0]
        por_fuente.setdefault(fuente, []).append(r)
    for fuente, archivos in sorted(por_fuente.items()):
        partes = []
        for a in archivos:
            try:
                d = pd.read_csv(a)
            except Exception as e:
                print(f"  {os.path.basename(a)}: ilegible ({str(e)[:40]})", flush=True)
                continue
            d = d[[c for c in COLS if c in d.columns]].copy()
            partes.append(d)
        if not partes:
            continue
        df = pd.concat(partes, ignore_index=True)
        df["modelo"] = df.modelo.astype(str).str.upper().str.strip()
        df["fecha"] = pd.to_datetime(df.fecha).dt.date.astype(str)
        for c in ("precio_lista", "descuento_pct", "precio_venta", "existencia"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        # PRECIOS EN USD (usuario 2026-08-07): convertir cuando la moneda sea
        # MXN/MN/pesos, con el tipo de cambio de la SEMANA de cada dato (panel).
        # La detección de cambios usa la moneda NATIVA (el FX no es un cambio).
        tcs, tc_rec = _tc_por_semana()
        sem = pd.to_datetime(df.fecha).dt.to_period("W-SUN").dt.start_time
        tc_fila = sem.map(tcs).fillna(tc_rec)
        es_mxn = df.moneda.astype(str).str.upper().str.contains(
            "MXN|PESO|^MN$|M\\.N", regex=True, na=False)
        df["precio_venta_usd"] = np.where(es_mxn, df.precio_venta / tc_fila,
                                          df.precio_venta).round(2)
        df["precio_lista_usd"] = np.where(es_mxn, df.precio_lista / tc_fila,
                                          df.precio_lista).round(2)
        ruta_db = os.path.join(DB, f"{fuente}.parquet")
        if os.path.exists(ruta_db):
            df = pd.concat([pd.read_parquet(ruta_db), df], ignore_index=True)
        df = (df.sort_values("fecha")
              .drop_duplicates(["modelo", "fecha"], keep="last")
              .reset_index(drop=True))
        df.to_parquet(ruta_db, index=False)
        print(f"  {fuente:<14} {df.modelo.nunique():>7,} modelos | "
              f"{df.fecha.nunique():>3} fechas | {len(df):>9,} filas → {ruta_db}",
              flush=True)


def cambios():
    """Detecta movimientos por competidor comparando fechas consecutivas."""
    filas = []
    for ruta_db in sorted(glob.glob(os.path.join(DB, "*.parquet"))):
        fuente = os.path.basename(ruta_db)[:-8]
        df = pd.read_parquet(ruta_db).sort_values(["modelo", "fecha"])
        df["pv_prev"] = df.groupby("modelo").precio_venta.shift(1)
        df["ex_prev"] = df.groupby("modelo").existencia.shift(1)
        df["fecha_prev"] = df.groupby("modelo").fecha.shift(1)
        primera = df.fecha.min()
        for x in df.itertuples():
            if pd.isna(x.pv_prev):
                if x.fecha != primera:
                    filas.append((fuente, x.fecha, x.modelo, "ALTA", np.nan,
                                  x.precio_venta, x.moneda))
                continue
            if x.pv_prev > 0 and pd.notna(x.precio_venta):
                d_ = x.precio_venta / x.pv_prev - 1
                if abs(d_) >= UMBRAL_PRECIO:
                    filas.append((fuente, x.fecha, x.modelo,
                                  "PRECIO SUBIÓ" if d_ > 0 else "PRECIO BAJÓ",
                                  round(100 * d_, 1), x.precio_venta, x.moneda))
            if pd.notna(x.ex_prev) and pd.notna(x.existencia):
                if x.ex_prev > 0 and x.existencia == 0:
                    filas.append((fuente, x.fecha, x.modelo, "STOCKOUT",
                                  np.nan, x.precio_venta, x.moneda))
                elif x.ex_prev == 0 and x.existencia > 0:
                    filas.append((fuente, x.fecha, x.modelo, "REABASTECIDO",
                                  np.nan, x.precio_venta, x.moneda))
    C = pd.DataFrame(filas, columns=["fuente", "fecha", "modelo", "tipo",
                                     "delta_pct", "precio_venta", "moneda"])
    C.to_parquet(os.path.join(CRUDOS, "cambios.parquet"), index=False)
    corte = str(pd.Timestamp.today().date() - pd.Timedelta(days=7))
    C[C.fecha >= corte].to_csv(os.path.join(BASE, "out", "competencia_cambios.csv"),
                               index=False)
    print(f"\ncambios detectados (historia): {len(C):,}", flush=True)
    if len(C):
        print(C.groupby(["fuente", "tipo"]).size().unstack(fill_value=0).to_string(),
              flush=True)
    return C


def actualizar():
    """Paso del cron diario: consolidar lo nuevo + re-detectar cambios."""
    consolidar()
    cambios()


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    {"consolidar": consolidar, "cambios": cambios}.get(modo, actualizar)()
