# -*- coding: utf-8 -*-
"""T2.7 / prioridad #5 del roadmap — Double ML como TERCER estimador de ε.

Partially Linear Regression (Robinson) con cross-fitting:
    Y = θ·D + g(X) + u          Y = log(unidades), D = log(precio_lista)
  1. ℓ(X) = E[Y|X] y m(X) = E[D|X] con GBM (nuisances), 5 folds POR SKU
     (cross-fitting: cada observación se predice con modelos que no la vieron).
  2. θ = Σ(D−m̂)(Y−ℓ̂) / Σ(D−m̂)²  (residuo-sobre-residuo). SE agrupado por SKU.

X (confusores): identidad del SKU vía medias propias (dispositivo Mundlak ≈ FE),
demanda REZAGADA (el canal de confusión #1: los precios se mueven en respuesta
a la demanda reciente — regresión a la media), tendencia y mes. El costo NO va
en X (es shifter de oferta: absorberlo mataría la variación buena).
ML nunca estima ε directo (regla del proyecto): solo los nuisances.

TRIANGULACIÓN (regla del CLAUDE.md): PPML, DML (y a futuro IV) coinciden en
signo y magnitud ⇒ confianza; divergen ⇒ los ε se tratan con cautela/abstención.

Salida: impresión + data/dml_eps.parquet (global y por segmento de rotación).
"""
import os

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
FOLDS = 5


def _prepara():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    pan["unidades"] = pan.unidades_rec.astype(float)
    pan = pan.sort_values(["codigo", "semana"])
    d = pan[pan.unidades > 0].copy()          # margen intensivo (PLR pide Y continua)
    d["Y"] = np.log(d.unidades)
    d["D"] = np.log(d.precio_lista.astype(float))
    # rezagos de demanda propios (confusor principal)
    g = pan.groupby("codigo")
    pan["lag1"] = g.unidades.shift(1)
    pan["lag4"] = g.unidades.shift(1).rolling(4).mean().reset_index(0, drop=True)
    d = d.merge(pan[["codigo", "semana", "lag1", "lag4"]], on=["codigo", "semana"])
    d["log_lag1"] = np.log1p(d.lag1.fillna(0))
    d["log_lag4"] = np.log1p(d.lag4.fillna(0))
    # dispositivo Mundlak: medias por SKU ≈ efectos fijos
    med = d.groupby("codigo").agg(Y_med=("Y", "mean"), D_med=("D", "mean"),
                                  n_obs_sku=("Y", "size")).reset_index()
    d = d.merge(med, on="codigo")
    sem = np.sort(d.semana.unique())
    t_idx = {s: i for i, s in enumerate(sem)}
    d["t"] = d.semana.map(t_idx)
    d["mes"] = pd.to_datetime(d.semana).dt.month
    # segmento de rotación (mismo del PPML, para comparar peras con peras)
    seg = pd.read_parquet(os.path.join(DATA, "eps_por_sku.parquet")).set_index("codigo")
    d["segmento"] = d.codigo.map(seg.segmento).fillna("sin")
    return d


X_COLS = ["Y_med", "D_med", "log_lag1", "log_lag4", "t", "mes", "n_obs_sku"]


def _dml_theta(d):
    """PLR con cross-fitting por SKU; devuelve (θ, se_cluster)."""
    skus = d.codigo.unique()
    rng = np.random.default_rng(7)
    fold_de = dict(zip(skus, rng.integers(0, FOLDS, len(skus))))
    d = d.assign(fold=d.codigo.map(fold_de))
    resY = np.empty(len(d))
    resD = np.empty(len(d))
    for f in range(FOLDS):
        tr, te = d.fold != f, d.fold == f
        for target, out in (("Y", resY), ("D", resD)):
            mod = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.08,
                                                max_depth=6, random_state=7)
            mod.fit(d.loc[tr, X_COLS], d.loc[tr, target])
            out[te.values] = d.loc[te, target] - mod.predict(d.loc[te, X_COLS])
    theta = (resD * resY).sum() / (resD ** 2).sum()
    # SE agrupado por SKU (score de Neyman)
    psi = resD * (resY - theta * resD)
    J = (resD ** 2).mean()
    grp = pd.Series(psi).groupby(d.codigo.values).sum()
    var = (grp ** 2).sum() / ((resD ** 2).sum()) ** 2
    return theta, float(np.sqrt(var))


def main():
    d = _prepara()
    print(f"panel DML (margen intensivo): {len(d):,} obs | {d.codigo.nunique():,} SKUs",
          flush=True)
    ppml = pd.read_parquet(os.path.join(DATA, "eps_por_sku.parquet"))
    seg_ppml = ppml.groupby("segmento").eps.mean()
    filas = []
    th, se = _dml_theta(d)
    print(f"\n== DML (PLR, cross-fitting {FOLDS} folds por SKU) ==", flush=True)
    print(f"  GLOBAL  θ = {th:+.3f} (se {se:.3f})   |  PPML global −1.048", flush=True)
    filas.append({"segmento": "global", "eps_dml": th, "se_dml": se})
    for s in ["bajo", "medio", "alto"]:
        ds = d[d.segmento == s]
        if len(ds) < 5000:
            continue
        th_s, se_s = _dml_theta(ds)
        ref = seg_ppml.get(s, np.nan)
        coincide = np.isfinite(ref) and (th_s < 0) and (0.5 <= th_s / ref <= 2.0)
        filas.append({"segmento": s, "eps_dml": th_s, "se_dml": se_s})
        print(f"  {s:<6}  θ = {th_s:+.3f} (se {se_s:.3f})   |  PPML {ref:+.3f}  "
              f"{'✓ TRIANGULA' if coincide else '✗ DIVERGE'}", flush=True)
    pd.DataFrame(filas).to_parquet(os.path.join(DATA, "dml_eps.parquet"), index=False)
    print("\nguardado data/dml_eps.parquet", flush=True)
    print("regla: signo y magnitud coinciden (ratio 0.5–2.0×) ⇒ los ε del PPML se "
          "sostienen; divergen ⇒ cautela/abstención (triangulación del CLAUDE.md; "
          "el tercer voto IV-costo sigue pendiente)", flush=True)


if __name__ == "__main__":
    main()
