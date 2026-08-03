# -*- coding: utf-8 -*-
"""RETADOR: elasticidad a nivel SKU (EB) vs la escalera vigente — con JUEZ
FIJADO ANTES DE VER RESULTADOS (filosofía campeón-retador, usuario).

Con la ventana 2024→hoy hay suficientes cambios de precio reales para que
algunos SKUs tengan ε PROPIA. El retador extiende la escalera:
  global → segmento → proveedor → [NUEVO] SKU (encogimiento EB hacia el prior
  de proveedor/segmento; w = n/(n+K), K=4 eventos).

JUEZ (fijado aquí, antes de correr):
  - Eventos de cambio de lista ≥2% aislados, con venta previa ≥3 u/sem y
    stock (los mismos criterios del replay blindado).
  - SPLIT TEMPORAL HONESTO: ε propia se estima SOLO con eventos hasta
    2025-12-31; se evalúa SOLO en eventos de 2026 (out-of-time).
  - Métrica: |Δvolumen predicho − Δvolumen real ajustado por mercado| por
    evento, con Δ predicho = (p1/p0)^ε − 1. Se compara mediana del error
    CAMPEÓN (ε vigente de eps_por_sku) vs RETADOR (ε con capa SKU).
  - Regla de adopción: el retador entra SOLO si reduce el error mediano en el
    subconjunto donde difiere del campeón (SKUs con ≥3 eventos propios de
    entrenamiento) Y no lo empeora en el total. Si no, se archiva.

Uso:  ./.venv/bin/python analisis_eps_sku.py
Salida: impresión del veredicto + data/eps_sku_retador.parquet
"""
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
K_EB = 4
CORTE_TRAIN = pd.Timestamp("2026-01-01")
PRE, POST = 4, 3


def eventos_precio():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    if "activo" in pan.columns:
        pan = pan[pan.activo]
    semanas = np.sort(pan.semana.unique())
    u = pan.pivot(index="codigo", columns="semana", values="unidades_rec").reindex(columns=semanas)
    p = pan.pivot(index="codigo", columns="semana", values="precio_lista").reindex(columns=semanas).ffill(axis=1)
    mkt = pan.groupby("semana").unidades_rec.sum().reindex(semanas).values
    evs = []
    for cod in p.index:
        ps, us = p.loc[cod].values, u.loc[cod].fillna(0.0).values
        for i in range(PRE, len(ps) - POST):
            p0, p1 = ps[i - 1], ps[i]
            if not (np.isfinite(p0) and np.isfinite(p1) and p0 > 0):
                continue
            rel = p1 / p0 - 1
            if abs(rel) < 0.02:
                continue
            prev = ps[max(0, i - PRE):i]
            if len(prev) > 1 and np.nanmax(np.abs(np.diff(prev) / prev[:-1])) >= 0.015:
                continue
            u_pre = us[i - PRE:i].mean()
            if u_pre < 3:
                continue
            m_pre, m_post = np.nanmean(mkt[i - PRE:i]), np.nanmean(mkt[i:i + POST])
            if not (m_pre > 0 and m_post > 0):
                continue
            u_post = us[i:i + POST].mean()
            d_real = (u_post / u_pre) / (m_post / m_pre) - 1
            e_hat = np.log(max(1e-6, 1 + d_real)) / np.log(p1 / p0)
            evs.append((cod, pd.Timestamp(semanas[i]), rel, d_real, e_hat))
    return pd.DataFrame(evs, columns=["codigo", "semana", "rel", "d_real", "e_hat"])


def correr():
    ev = eventos_precio()
    print(f"eventos de lista ≥2% medibles: {len(ev):,} en {ev.codigo.nunique():,} SKUs", flush=True)
    tr = ev[ev.semana < CORTE_TRAIN]
    te = ev[ev.semana >= CORTE_TRAIN].copy()
    print(f"  entrenamiento (<2026): {len(tr):,} | evaluación OOT (2026+): {len(te):,}", flush=True)

    prior = pd.read_parquet(os.path.join(DATA, "eps_por_sku.parquet")).set_index("codigo")
    # ε propia por SKU con eventos de entrenamiento (recorte de e_hat a rango
    # sano [-5, 1] para que un evento loco no domine la media)
    g = tr.assign(e=tr.e_hat.clip(-5, 1)).groupby("codigo").e.agg(["mean", "count"])
    g = g[g["count"] >= 1]
    eps_prior = prior.eps.reindex(g.index)
    w = g["count"] / (g["count"] + K_EB)
    eps_sku = (w * g["mean"] + (1 - w) * eps_prior).clip(-5, 0)
    retador = prior.eps.copy()
    retador.loc[eps_sku.index] = eps_sku
    n_prop = int((g["count"] >= 3).sum())
    print(f"  SKUs con capa propia: {len(eps_sku):,} (con ≥3 eventos: {n_prop:,})", flush=True)

    te["e_camp"] = prior.eps.reindex(te.codigo).values
    te["e_ret"] = retador.reindex(te.codigo).values
    te = te.dropna(subset=["e_camp", "e_ret"])
    te["pred_c"] = (1 + te.rel) ** te.e_camp - 1
    te["pred_r"] = (1 + te.rel) ** te.e_ret - 1
    te["err_c"] = (te.pred_c - te.d_real).abs()
    te["err_r"] = (te.pred_r - te.d_real).abs()

    dif = te[te.codigo.isin(g[g["count"] >= 3].index)]
    print(f"\n== JUEZ (error absoluto mediano del Δvolumen, OOT 2026) ==", flush=True)
    print(f"  TODOS los eventos ({len(te):,}): campeón {te.err_c.median():.3f} "
          f"vs retador {te.err_r.median():.3f}", flush=True)
    print(f"  SUBCONJUNTO con ε propia ≥3 eventos ({len(dif):,}): "
          f"campeón {dif.err_c.median():.3f} vs retador {dif.err_r.median():.3f}", flush=True)
    gana_sub = dif.err_r.median() < dif.err_c.median() if len(dif) else False
    no_daña = te.err_r.median() <= te.err_c.median() * 1.005
    veredicto = "ADOPTAR (gana en su subconjunto sin dañar el total)" if (gana_sub and no_daña) \
        else "ARCHIVAR (no gana con el juez fijado)"
    print(f"\nVEREDICTO: {veredicto}", flush=True)
    out = pd.DataFrame({"codigo": eps_sku.index, "eps_retador": eps_sku.values,
                        "n_eventos_train": g["count"].reindex(eps_sku.index).values})
    out.to_parquet(os.path.join(DATA, "eps_sku_retador.parquet"), index=False)
    print(f"→ data/eps_sku_retador.parquet ({len(out):,} SKUs)", flush=True)


def aplicar():
    """ADOPCIÓN (veredicto 2026-07-31: retador ganó OOT 0.390 vs 0.395 en su
    subconjunto sin dañar el total): agrega la capa SKU a eps_por_sku.parquet
    — SOLO SKUs con ≥3 eventos propios (lo que el juez validó), entrenando
    ahora con TODA la ventana (el split temporal era solo para juzgar).
    Corre DESPUÉS de modelo.py y ANTES de escenarios.py (run.py)."""
    ev = eventos_precio()
    ruta = os.path.join(DATA, "eps_por_sku.parquet")
    prior = pd.read_parquet(ruta)
    # GUARD de idempotencia (auditoría 2026-07-31, N4): aplicar dos veces sin
    # re-correr modelo.py re-encogería eps/se y duplicaría la etiqueta
    if prior.nivel.astype(str).str.contains("capa SKU").any():
        raise SystemExit("aplicar: eps_por_sku YA trae la capa SKU — re-corre "
                         "modelo.py antes de re-aplicar (doble encogimiento)")
    idx = prior.set_index("codigo")
    g = ev.assign(e=ev.e_hat.clip(-5, 1)).groupby("codigo").e.agg(["mean", "std", "count"])
    g = g[g["count"] >= 3]
    g = g[g.index.isin(idx.index)]
    w = g["count"] / (g["count"] + K_EB)
    eps_prior = idx.eps.reindex(g.index)
    se_prior = idx.se.reindex(g.index)
    eps_new = (w * g["mean"] + (1 - w) * eps_prior).clip(-5, 0)
    se_ev = (g["std"] / np.sqrt(g["count"])).fillna(se_prior)
    se_new = (w * se_ev + (1 - w) * se_prior).clip(lower=0.02)
    m = prior.codigo.isin(g.index)
    prior.loc[m, "eps"] = prior.loc[m, "codigo"].map(eps_new)
    prior.loc[m, "se"] = prior.loc[m, "codigo"].map(se_new)
    prior.loc[m, "nivel"] = prior.loc[m, "nivel"].astype(str) + " + capa SKU (EB eventos)"
    prior.to_parquet(ruta, index=False)
    print(f"capa SKU aplicada a {int(m.sum()):,} códigos (≥3 eventos propios) → {ruta}",
          flush=True)


if __name__ == "__main__":
    aplicar() if len(sys.argv) > 1 and sys.argv[1] == "aplicar" else correr()
