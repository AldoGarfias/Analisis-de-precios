# -*- coding: utf-8 -*-
"""COMPORTAMIENTO DE COSTOS → PRECIOS → VENTAS (usuario 2026-07-31, con la
ventana ampliada 2024→hoy): estudio de eventos REALES de cambio de costo.

Para cada evento de cambio de costo de proveedor ≥2% (el umbral de la vigía y
la defensa de margen) con historia suficiente alrededor:

  1. ¿SE TRASLADÓ AL PRECIO? — pass-through = Δlista / Δcosto en las 4 semanas
     siguientes (0 = se absorbió, 1 = se trasladó completo).
  2. ¿CON QUÉ REZAGO? — semanas entre el cambio de costo y el primer movimiento
     de lista ≥1% posterior (máx 8 semanas de búsqueda).
  3. ¿QUÉ LE PASÓ A LA VENTA? — Δ% de unidades (4 semanas después vs 4 antes),
     ajustado por mercado, separando eventos TRASLADADOS (pt ≥ 0.5) vs
     ABSORBIDOS (pt < 0.5). También el Δ margen unitario realizado.

Salidas: impresión del estudio + data/analisis_costos.parquet (evento por
evento, insumo para reglas futuras — p.ej. calibrar el chip 🔺 COSTO con
evidencia: "históricamente un traslado completo de +5% de costo costó X% de
volumen; absorberlo costó Y pts de margen").

Uso:  ./.venv/bin/python analisis_costos.py
"""
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

UMBRAL_COSTO = 0.02   # mismo de vigía/defensa
UMBRAL_LISTA = 0.01   # movimiento de lista "real"
PRE, POST, LAG_MAX = 4, 4, 8


def correr():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy() if "activo" in pan.columns else pan
    semanas = np.sort(pan.semana.unique())
    u = pan.pivot(index="codigo", columns="semana", values="unidades_rec").reindex(columns=semanas)
    p = pan.pivot(index="codigo", columns="semana", values="precio_lista").reindex(columns=semanas).ffill(axis=1)
    c = pan.pivot(index="codigo", columns="semana", values="costo_prom").reindex(columns=semanas).ffill(axis=1)
    neto = pan.pivot(index="codigo", columns="semana", values="neto_prom").reindex(columns=semanas)
    mkt = pan.groupby("semana").unidades_rec.sum().reindex(semanas).values

    eventos = []
    for cod in c.index:
        cs, ps, us = c.loc[cod].values, p.loc[cod].values, u.loc[cod].fillna(0.0).values
        ns = neto.loc[cod].values if cod in neto.index else np.full(len(cs), np.nan)
        for i in range(PRE, len(cs) - POST):
            c0, c1 = cs[i - 1], cs[i]
            if not (np.isfinite(c0) and np.isfinite(c1) and c0 > 0):
                continue
            d_c = c1 / c0 - 1
            if abs(d_c) < UMBRAL_COSTO:
                continue
            # evento aislado: sin otro cambio de costo ≥2% en las PRE semanas previas
            prev = cs[max(0, i - PRE):i]
            if len(prev) > 1 and np.nanmax(np.abs(np.diff(prev) / prev[:-1])) >= UMBRAL_COSTO:
                continue
            p0 = ps[i - 1]
            if not (np.isfinite(p0) and p0 > 0):
                continue
            # pass-through a 4 semanas y rezago del primer movimiento de lista
            p_post = ps[i:i + POST + 1]
            d_p4 = (p_post[-1] / p0 - 1) if np.isfinite(p_post[-1]) else np.nan
            pt = d_p4 / d_c if np.isfinite(d_p4) and d_c != 0 else np.nan
            lag = np.nan
            for k in range(0, min(LAG_MAX, len(ps) - i - 1)):
                if np.isfinite(ps[i + k]) and abs(ps[i + k] / p0 - 1) >= UMBRAL_LISTA:
                    lag = k
                    break
            # respuesta de la venta, ajustada por mercado
            u_pre, u_post = us[i - PRE:i].mean(), us[i:i + POST].mean()
            m_pre, m_post = np.nanmean(mkt[i - PRE:i]), np.nanmean(mkt[i:i + POST])
            if u_pre <= 0 or not (m_pre > 0 and m_post > 0):
                continue
            d_u = (u_post / u_pre) / (m_post / m_pre) - 1
            # margen unitario realizado (neto − costo)
            n_pre = np.nanmean(ns[i - PRE:i]); n_post = np.nanmean(ns[i:i + POST])
            marg_pre = (n_pre - c0) / n_pre if np.isfinite(n_pre) and n_pre > 0 else np.nan
            marg_post = (n_post - c1) / n_post if np.isfinite(n_post) and n_post > 0 else np.nan
            eventos.append((cod, pd.Timestamp(semanas[i]), round(100 * d_c, 1),
                            round(pt, 2) if np.isfinite(pt) else np.nan,
                            lag, round(100 * d_u, 1),
                            round(100 * (marg_post - marg_pre), 1)
                            if np.isfinite(marg_post) and np.isfinite(marg_pre) else np.nan,
                            round(u_pre, 1)))
    ev = pd.DataFrame(eventos, columns=["codigo", "semana", "d_costo_pct", "pass_through",
                                        "lag_sem", "d_venta_pct", "d_margen_pts", "u_pre"])
    ev.to_parquet(os.path.join(DATA, "analisis_costos.parquet"), index=False)

    print(f"== COMPORTAMIENTO DE COSTOS → PRECIO → VENTA ({len(semanas)} semanas) ==", flush=True)
    print(f"eventos de costo ≥±2% aislados y medibles: {len(ev):,} "
          f"({ev.codigo.nunique():,} SKUs)", flush=True)
    for signo, m in [("SUBIÓ", ev.d_costo_pct > 0), ("BAJÓ", ev.d_costo_pct < 0)]:
        e = ev[m & (ev.u_pre >= 3)]  # con venta real para medir respuesta
        if e.empty:
            continue
        pt_med = e.pass_through.median()
        con_lag = e.lag_sem.notna().mean()
        lag_med = e.lag_sem.median()
        print(f"\nCOSTO {signo} (n={len(e):,}, venta previa ≥3 u/sem):", flush=True)
        print(f"  pass-through a 4 sem: mediana {pt_med:+.2f} "
              f"(p25 {e.pass_through.quantile(.25):+.2f} / p75 {e.pass_through.quantile(.75):+.2f})",
              flush=True)
        print(f"  la lista se movió ≥1% en ≤8 sem: {100*con_lag:.0f}% de los eventos "
              f"(rezago mediano {lag_med:.0f} sem)", flush=True)
        tras = e[e.pass_through >= 0.5]
        abso = e[e.pass_through.fillna(0) < 0.5]
        print(f"  TRASLADADOS (pt≥0.5, n={len(tras):,}): venta {tras.d_venta_pct.median():+.1f}% "
              f"| margen {tras.d_margen_pts.median():+.1f} pts", flush=True)
        print(f"  ABSORBIDOS  (pt<0.5, n={len(abso):,}): venta {abso.d_venta_pct.median():+.1f}% "
              f"| margen {abso.d_margen_pts.median():+.1f} pts", flush=True)
    print(f"\nevento por evento → data/analisis_costos.parquet", flush=True)
    return ev


if __name__ == "__main__":
    correr()
