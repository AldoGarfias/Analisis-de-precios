# -*- coding: utf-8 -*-
"""T2.5 — Chronos-Bolt (modelo fundacional, zero-shot) como retador de u0.

Mismo examen FVA que todos: 2 ventanas out-of-time, RMSSE por clase de serie,
contra el u0 vigente (GBM residual / SBA por clase). Chronos-Bolt corre local
(CPU), produce cuantiles en un forward pass y NO conoce nuestro negocio —
si aún así gana en alguna clase, esa clase tiene un problema de features.

Uso:  ./.venv/bin/python chronos_examen.py [small|base]
"""
import os
import sys

import numpy as np
import pandas as pd
import torch

import forecast as fc

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
H = fc.H
CONTEXTO = 64          # semanas de historia que ve Chronos
LOTE = 512


def correr(tam="small"):
    from chronos import BaseChronosPipeline
    pipe = BaseChronosPipeline.from_pretrained(f"amazon/chronos-bolt-{tam}",
                                               device_map="cpu",
                                               torch_dtype=torch.float32)
    mdf, p, semanas = fc._cargar_matrices()
    m = mdf.values
    n_sem = m.shape[1]
    clase = (pd.read_parquet(os.path.join(DATA, "adi_cv2.parquet"))
             .set_index("codigo").clase.reindex(mdf.index).fillna("sin clase").values)

    regs = []
    for k in range(2):
        corte = n_sem - H - 1 - k * 6
        print(f"ventana {k+1}/2 (corte {pd.Timestamp(semanas[corte]).date()})", flush=True)
        # u0 vigente (GBM/SBA por clase), reentrenado al corte (sin fuga)
        mod = fc._fit(m, p, semanas, corte)
        pred = fc._predice(mod, m, p, semanas, corte)
        usa_sba = np.isin(clase, ["errática", "grumosa (lumpy)"])
        pred = np.where(usa_sba, fc._sba_por_corte(m, corte), pred)
        # Chronos-Bolt zero-shot: mediana de los 3 pasos
        ctx = torch.tensor(m[:, max(0, corte - CONTEXTO + 1): corte + 1],
                           dtype=torch.float32)
        preds_c = []
        for j in range(0, ctx.shape[0], LOTE):
            q, _ = pipe.predict_quantiles(ctx[j: j + LOTE], prediction_length=H,
                                          quantile_levels=[0.5])
            preds_c.append(q[:, :, 0].mean(dim=1).numpy())
        pred_c = np.clip(np.concatenate(preds_c), 0, None)
        real = m[:, corte + 1: corte + 1 + H].mean(axis=1)
        dif = np.diff(m[:, :corte + 1], axis=1)
        esc = np.sqrt((dif ** 2).mean(axis=1))
        esc[esc == 0] = np.nan
        regs.append(pd.DataFrame({"clase": clase, "real": real, "esc": esc,
                                  "vigente": pred, "chronos": pred_c}))
    ev = pd.concat(regs, ignore_index=True)
    ev.to_parquet(os.path.join(DATA, "examen_chronos.parquet"), index=False)

    print(f"\n== CHRONOS-BOLT-{tam.upper()} (zero-shot) vs u0 VIGENTE — RMSSE ==", flush=True)
    for cl, g in ev.groupby("clase"):
        r_v = np.nanmean(np.abs(g.vigente - g.real) / g.esc)
        r_c = np.nanmean(np.abs(g.chronos - g.real) / g.esc)
        w_v = (g.vigente - g.real).abs().sum() / max(g.real.sum(), 1e-9)
        w_c = (g.chronos - g.real).abs().sum() / max(g.real.sum(), 1e-9)
        print(f"  {cl:<20} vigente {r_v:.3f} (WAPE {w_v:.3f}) | "
              f"chronos {r_c:.3f} (WAPE {w_c:.3f})  "
              f"{'⚠ CHRONOS GANA' if r_c < r_v else 'vigente ✓'}", flush=True)


if __name__ == "__main__":
    correr(sys.argv[1] if len(sys.argv) > 1 else "small")
