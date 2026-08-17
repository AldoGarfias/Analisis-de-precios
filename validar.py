# -*- coding: utf-8 -*-
"""Backtest temporal (out-of-time) del pronóstico base de ventas observables.

CAPA 1 + CAPA 4 del enfoque de escenarios:
  - Elige el pronóstico base u0 por COMPETENCIA en holdout temporal, no por teoría:
    candidatos simples (última semana, media 4/8/12 semanas). Un modelo ML entrará
    solo si le gana a estos (regla del proyecto: si no le gana al promedio, no entra).
  - La incertidumbre se mide EMPÍRICAMENTE: cuantiles del ratio real/pronóstico por
    tercil de volumen en el holdout. Sin supuestos distribucionales.

Protocolo anti-fuga: el pronóstico de las semanas de holdout usa SOLO semanas
anteriores al corte. Semana sin fila en el holdout para un SKU activo = 0 ventas
observadas (es pronóstico de venta observable, NO de demanda: sin inventario no
se puede distinguir cero-demanda de cero-disponibilidad).

Salida: data/backtest.parquet (método ganador + multiplicadores p10/p50/p90 por
tercil de volumen) y métricas impresas.
"""
import os

import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PANEL = os.path.join(DATA, "panel.parquet")
OUT = os.path.join(DATA, "backtest.parquet")

H = 1  # semanas de holdout (minimal; usa prácticamente todos los datos; 2026-08-14)


def _matriz(pan):
    """codigo x semana -> unidades (0 = sin venta observada esa semana)."""
    m = pan.pivot_table(index="codigo", columns="semana", values="unidades",
                        aggfunc="sum").fillna(0.0)
    return m.sort_index(axis=1)


def _candidatos(train):
    """Pronósticos u0 (unidades/semana) con datos SOLO del train."""
    return {
        "ultima_sem": train.iloc[:, -1],
        "media_4s": train.iloc[:, -4:].mean(axis=1),
        "media_8s": train.iloc[:, -8:].mean(axis=1),
        "media_12s": train.iloc[:, -12:].mean(axis=1),
    }


def correr_backtest(pan):
    act = pan[pan.activo].copy()
    if "unidades_rec" in act.columns:  # demanda recurrente (sin proyectos)
        act["unidades"] = act["unidades_rec"].astype(float)
    m = _matriz(act)
    if m.shape[1] < H + 6:
        raise SystemExit(f"Historia insuficiente para backtest: {m.shape[1]} semanas")
    train, hold = m.iloc[:, :-H], m.iloc[:, -H:]
    real = hold.mean(axis=1)  # unidades/semana promedio en el holdout

    print(f"backtest: {m.shape[0]:,} SKUs | train {train.shape[1]} sem | holdout {H} sem "
          f"({hold.columns[0].date()} → {hold.columns[-1].date()})", flush=True)

    res = {}
    for nombre, pred in _candidatos(train).items():
        err = (pred - real).abs()
        wape = err.sum() / real.sum()
        sesgo = (pred - real).sum() / real.sum()
        res[nombre] = (wape, sesgo, pred)
        print(f"  {nombre:<11} WAPE {wape:.3f}  sesgo {sesgo:+.3f}", flush=True)

    ganador = min(res, key=lambda k: res[k][0])
    wape_g, sesgo_g, pred_g = res[ganador]
    print(f"  → ganador: {ganador} (WAPE {wape_g:.3f})", flush=True)

    # incertidumbre empírica: cuantiles de real/pred por tercil de volumen
    df = pd.DataFrame({"pred": pred_g, "real": real})
    df = df[df.pred > 0]
    df["ratio"] = df.real / df.pred
    df["tercil"] = pd.qcut(df.pred, 3, labels=["bajo", "medio", "alto"])
    bandas = df.groupby("tercil", observed=True).ratio.quantile([.1, .5, .9]).unstack()
    bandas.columns = ["p10", "p50", "p90"]
    print("\n  bandas empíricas real/pronóstico por tercil de volumen:", flush=True)
    print(bandas.round(2).to_string(), flush=True)

    out = bandas.reset_index()
    out["metodo_u0"] = ganador
    out["wape"] = wape_g
    out["sesgo"] = sesgo_g
    out.to_parquet(OUT, index=False)
    print(f"\nguardado {OUT}", flush=True)
    return ganador, bandas


if __name__ == "__main__":
    correr_backtest(pd.read_parquet(PANEL))
