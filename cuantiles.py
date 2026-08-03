# -*- coding: utf-8 -*-
"""T2.2 — Bandas CONDICIONALES por SKU (GBM cuantil) vs bandas por tercil.

Hoy la banda p10–p90 de un SKU es la de su TERCIL de volumen (3 bandas para
12,594 SKUs). Aquí dos GBM con loss="quantile" (α=0.1 y 0.9) aprenden la banda
POR SKU condicional a sus features (lags, media4, precio, mes...).

Juez (regla acordada): cobertura fuera de muestra — el 80% de las semanas
reales deben caer dentro de p10–p90, POR CLASE de serie. Gana quien se acerque
más a 80% sin inflar el ancho. Si el cuantil condicional no gana, no entra.

Salida: veredicto impreso + data/examen_cuantiles.parquet.
"""
import os

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

import forecast as fc

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
H = fc.H


def _fit_q(m, p, semanas, corte, alfa):
    cortes_train = [t for t in range(16, corte - H, 2)]
    X_tr = pd.concat([fc._features(m, p, t, semanas) for t in cortes_train],
                     ignore_index=True)
    y_tr = np.concatenate([m[:, t + 1: t + 1 + H].mean(axis=1) for t in cortes_train])
    mod = HistGradientBoostingRegressor(loss="quantile", quantile=alfa, max_iter=250,
                                        learning_rate=0.08, max_depth=6, random_state=7)
    mod.fit(X_tr, y_tr)
    return mod


def correr(n_ventanas=2):
    mdf, p, semanas = fc._cargar_matrices()
    m = mdf.values
    n_sem = m.shape[1]
    clase = (pd.read_parquet(os.path.join(DATA, "adi_cv2.parquet"))
             .set_index("codigo").clase.reindex(mdf.index).fillna("sin clase").values)
    terc_b = pd.read_parquet(os.path.join(DATA, "forecast_bandas.parquet"))
    u0_df = pd.read_parquet(os.path.join(DATA, "forecast_u0.parquet")).set_index("codigo")

    regs = []
    for k in range(n_ventanas):
        corte = n_sem - H - 1 - k * 6
        print(f"ventana {k+1}/{n_ventanas} (corte {pd.Timestamp(semanas[corte]).date()}): "
              f"entrenando cuantiles…", flush=True)
        q10 = _fit_q(m, p, semanas, corte, 0.10)
        q90 = _fit_q(m, p, semanas, corte, 0.90)
        X_te = fc._features(m, p, corte, semanas)
        lo = np.maximum(q10.predict(X_te), 0.0)
        hi = np.maximum(q90.predict(X_te), lo)
        real = m[:, corte + 1: corte + 1 + H].mean(axis=1)
        # bandas actuales (tercil): u0 puntual del método vigente × ratios del tercil
        mod_pt = fc._fit(m, p, semanas, corte)
        pt = fc._predice(mod_pt, m, p, semanas, corte)
        usa_sba = np.isin(clase, ["errática", "grumosa (lumpy)"])
        pt = np.where(usa_sba, fc._sba_por_corte(m, corte), pt)
        tercil = pd.qcut(pd.Series(pt).rank(method="first"), 3,
                         labels=["bajo", "medio", "alto"]).astype(str).values
        tb = terc_b.set_index("tercil")
        lo_t = pt * np.array([tb.p10.get(t, 0) for t in tercil])
        hi_t = pt * np.array([tb.p90.get(t, 2) for t in tercil])
        regs.append(pd.DataFrame({"clase": clase, "real": real,
                                  "lo_q": lo, "hi_q": hi, "lo_t": lo_t, "hi_t": hi_t}))
    ev = pd.concat(regs, ignore_index=True)
    ev.to_parquet(os.path.join(DATA, "examen_cuantiles.parquet"), index=False)

    print(f"\n== EXAMEN DE BANDAS (objetivo de cobertura: 80%) ==", flush=True)
    print(f"{'clase':<20}{'cobertura TERCIL':>18}{'ancho':>8}{'cobertura CUANTIL':>19}{'ancho':>8}  veredicto",
          flush=True)
    gana_q = 0
    for cl, g in ev.groupby("clase"):
        cob_t = ((g.real >= g.lo_t) & (g.real <= g.hi_t)).mean()
        cob_q = ((g.real >= g.lo_q) & (g.real <= g.hi_q)).mean()
        an_t = (g.hi_t - g.lo_t).median()
        an_q = (g.hi_q - g.lo_q).median()
        mejor = abs(cob_q - 0.80) < abs(cob_t - 0.80)
        gana_q += int(mejor)
        print(f"{cl:<20}{100*cob_t:>16.1f}%{an_t:>8.1f}{100*cob_q:>17.1f}%{an_q:>8.1f}"
              f"  {'CUANTIL ✓' if mejor else 'tercil ✓'}", flush=True)
    cob_t = ((ev.real >= ev.lo_t) & (ev.real <= ev.hi_t)).mean()
    cob_q = ((ev.real >= ev.lo_q) & (ev.real <= ev.hi_q)).mean()
    print(f"{'GLOBAL':<20}{100*cob_t:>16.1f}%{'':>8}{100*cob_q:>17.1f}%", flush=True)
    print(f"\nveredicto: cuantil condicional gana en {gana_q} clases", flush=True)


if __name__ == "__main__":
    correr()
