# -*- coding: utf-8 -*-
"""Estima la elasticidad-precio causal de la demanda por SKU.

Estrategia (identificación > forma funcional):
  - Panel SKU×semana; variable de precio = precio de lista `p1` (la palanca).
  - Efectos fijos de SKU y semana (within-transform de dos vías) para que ε se
    identifique de la variación de precio DENTRO del mismo SKU en el tiempo.
  - Endogeneidad: p1 se instrumenta con costo USD (`cos_prom_dlls`) y FX macro
    (choques exógenos a la demanda de un SKU). IV2SLS de linearmodels.
  - Estimación por marca (robusta, mucha data) y por cluster de similares; el ε de
    cada SKU se encoge (Empirical Bayes) hacia su cluster con su propia señal.

La demanda se modela log-lineal (log1p de unidades) por robustez y velocidad; PPML
(Poisson con FE) queda como refinamiento futuro (ver README). Guardas: F de
instrumento débil (<10) o signo positivo -> segmento no identificado, SKUs a baja
confianza (el optimizador los deja en MANTENER).

Salida: data/elast/modelo.parquet (id_art, eps, eps_se, componentes, confianza).
"""
import os
import warnings

import numpy as np
import pandas as pd
from linearmodels.iv import IV2SLS

warnings.filterwarnings("ignore")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "elast")

EPS_MIN, EPS_MAX = -5.0, -0.15   # rango económico admisible
F_MIN = 10.0                     # umbral de instrumento no-débil
MIN_SEM_SKU = 10                 # semanas con venta para intentar ε propio del SKU
MIN_CAMB_SKU = 3                 # cambios de p1 para intentar ε propio del SKU


def _demean2(df, cols, tkey="mes", iters=4):
    """Within-transform de dos vías (SKU y tiempo), iterativo para panel desbalanceado.

    Tiempo = `mes` (no semana): controla estacionalidad/macro sin absorber toda la
    variación de precio (los cambios de lista son lentos). El FX macro es un puro
    efecto de tiempo y queda absorbido -> el instrumento útil es el costo del SKU.
    """
    w = df[cols].astype(float).copy()
    w["_e"] = df["id_art"].values
    w["_t"] = df[tkey].values
    for _ in range(iters):
        w[cols] = w[cols] - w.groupby("_e")[cols].transform("mean")
        w[cols] = w[cols] - w.groupby("_t")[cols].transform("mean")
    return w[cols]


def _fe_iv(sub):
    """Elasticidad FE sobre un subconjunto del panel.

    Estimador primario = FE-OLS (el precio de LISTA es administrado por el PM, poco
    endógeno a la demanda semanal). Si el costo es instrumento fuerte (F≥10) se usa
    IV para quitar sesgo residual. Devuelve dict o None.
    """
    cols = ["log_unidades", "log_p1", "log_costo", "log_psust"]
    s = sub.dropna(subset=["log_unidades", "log_p1", "log_costo"]).copy()
    if s["id_art"].nunique() < 2 or s["mes"].nunique() < 3 or len(s) < 30:
        return None
    s["log_psust"] = s["log_psust"].fillna(0.0)  # sin sustituto -> término neutro
    d = _demean2(s, cols)
    if d["log_p1"].std() < 1e-4:
        return None  # sin variación de precio identificable
    # sólo incluir el cross-price si tiene variación tras demean (si no, es columna
    # cero -> IV2SLS truena por rango incompleto y la marca desaparecía a MANTENER)
    exog_c = ["log_psust"] if d["log_psust"].std() > 1e-6 else []
    # SE clusterizados por SKU: corrigen la correlación serial dentro del SKU (los
    # FE absorbidos por demean inflarían la significancia con SE robustos simples)
    cl = s["id_art"].values
    try:
        ols = IV2SLS(d["log_unidades"], d[["log_p1"] + exog_c], None, None).fit(
            cov_type="clustered", clusters=cl)
        eps_ols = float(ols.params["log_p1"])
        se_ols = float(ols.std_errors["log_p1"])
    except Exception:
        return None
    eps_iv = se_iv = F = np.nan
    if d["log_costo"].std() > 1e-6:
        try:
            iv = IV2SLS(d["log_unidades"], d[exog_c] if exog_c else None,
                        d[["log_p1"]], d[["log_costo"]]).fit(
                cov_type="clustered", clusters=cl)
            eps_iv = float(iv.params["log_p1"])
            se_iv = float(iv.std_errors["log_p1"])
            F = float(iv.first_stage.diagnostics.loc["log_p1", "f.stat"])
        except Exception:
            pass
    # preferir IV si el instrumento es fuerte, signo correcto y significativo
    if (not np.isnan(F) and F >= F_MIN and eps_iv < 0
            and se_iv > 0 and abs(eps_iv / se_iv) >= 1.64):
        eps, se, metodo = eps_iv, se_iv, "iv"
    else:
        eps, se, metodo = eps_ols, se_ols, "ols"
    return dict(eps=eps, se=se, F=F, n=len(s), metodo=metodo,
                eps_ols=eps_ols, se_ols=se_ols, eps_iv=eps_iv)


def _identificado(r):
    """ε creíble: signo correcto y pendiente significativa (|t|≥1.64, 90%) con SE
    clusterizados. Aplica tanto a IV como a OLS (la selección IV ya exige F≥10)."""
    if r is None or r["eps"] >= 0:
        return False
    return r["se"] > 0 and abs(r["eps"] / r["se"]) >= 1.64


def _eps_sku(g):
    """ε propio del SKU: OLS log_unidades ~ log_p1 + tendencia. (eps, se) o None."""
    g = g.dropna(subset=["log_unidades", "log_p1"])
    if g["p1"].nunique() < MIN_CAMB_SKU or (g["piezas"].fillna(0) > 0).sum() < MIN_SEM_SKU:
        return None
    if g["log_p1"].std() < 1e-6:
        return None
    tr = (g["week"].rank(method="dense") - 1).astype(float)
    X = np.column_stack([np.ones(len(g)), g["log_p1"].values, tr.values])
    y = g["log_unidades"].values
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        dof = max(len(g) - X.shape[1], 1)
        s2 = (resid @ resid) / dof
        xtx_inv = np.linalg.inv(X.T @ X)
        se = float(np.sqrt(s2 * xtx_inv[1, 1]))
        return float(beta[1]), se
    except Exception:
        return None


def estimar():
    panel = pd.read_parquet(f"{DATA}/panel.parquet")
    base = pd.read_parquet(f"{DATA}/baseline.parquet")

    # --- ε por marca (segmento robusto)
    eps_marca, meta_marca = {}, {}
    for marca, sub in panel.groupby("marca"):
        r = _fe_iv(sub)
        if r:
            ident = _identificado(r)
            eps_marca[marca] = r["eps"] if ident else np.nan
            meta_marca[marca] = dict(se=r["se"], F=r["F"], n=r["n"], ident=ident,
                                     eps_raw=r["eps"], metodo=r["metodo"])
            print(f"  marca {marca:20s} eps={r['eps']:+.2f} se={r['se']:.2f} "
                  f"F={r['F'] if not np.isnan(r['F']) else 0:6.1f} n={r['n']} "
                  f"[{r['metodo']}] {'OK' if ident else 'no-ident'}", flush=True)
        else:
            meta_marca[marca] = dict(se=np.nan, F=np.nan, n=0, ident=False,
                                     eps_raw=np.nan, metodo="-")
            print(f"  marca {marca:20s} sin variación identificable", flush=True)
    # prior global (mediana de marcas identificadas) para segmentos débiles
    ident_vals = [v for v in eps_marca.values() if not np.isnan(v)]
    eps_prior = float(np.median(ident_vals)) if ident_vals else -1.5

    # --- ε por cluster (donde haya señal); si no, hereda de marca
    eps_cluster = {}
    tam = panel.groupby("cluster_id")["id_art"].nunique()
    for cl in tam[tam >= 3].index:
        r = _fe_iv(panel[panel["cluster_id"] == cl])
        if r and _identificado(r):
            eps_cluster[cl] = dict(eps=r["eps"], se=r["se"])

    # --- ε propio por SKU + Empirical-Bayes shrinkage hacia el cluster
    filas = []
    sku_eps = {aid: _eps_sku(g) for aid, g in panel.groupby("id_art")}
    # varianza entre-SKU dentro de cada cluster (τ²) para el peso EB
    tau2 = {}
    for cl, g in base.groupby("cluster_id"):
        vals = [sku_eps[a][0] for a in g["id_art"] if sku_eps.get(a)]
        tau2[cl] = float(np.var(vals)) if len(vals) >= 3 else 0.25

    for row in base.itertuples(index=False):
        aid, marca, cl = int(row.id_art), row.marca, row.cluster_id
        # prior del SKU: cluster -> marca -> global
        if cl in eps_cluster:
            eps_pr, se_pr = eps_cluster[cl]["eps"], eps_cluster[cl]["se"]
            origen_pr = "cluster"
        elif not np.isnan(eps_marca.get(marca, np.nan)):
            eps_pr, se_pr = eps_marca[marca], meta_marca[marca]["se"]
            origen_pr = "marca"
        else:
            eps_pr, se_pr, origen_pr = eps_prior, 1.0, "global"

        own = sku_eps.get(aid)
        if own and own[0] < 0:
            eps_own, se_own = own
            t2 = max(tau2.get(cl, 0.25), 1e-3)
            w = t2 / (t2 + se_own ** 2)              # peso EB (precisión)
            eps = w * eps_own + (1 - w) * eps_pr
            se = np.sqrt(w ** 2 * se_own ** 2 + (1 - w) ** 2 * se_pr ** 2)
            fuente = f"eb({origen_pr})"
        else:
            eps, se, w = eps_pr, se_pr, 0.0
            eps_own = np.nan
            fuente = origen_pr

        eps = float(np.clip(eps, EPS_MIN, EPS_MAX))
        # Sólo se acciona donde la MARCA está identificada (IV limpio, ε<0). Si el
        # signo de la marca es positivo/débil, toda la marca está confundida
        # (precios demand-driven) -> abstenerse aunque un cluster/SKU dé señal ruidosa.
        ident = not np.isnan(eps_marca.get(marca, np.nan))
        if not ident:
            conf = "baja"
        elif se <= 0.4 and (fuente.startswith("eb") or origen_pr == "cluster"):
            conf = "alta"
        else:
            conf = "media"
        filas.append(dict(
            id_art=aid, marca=marca, cluster_id=cl,
            eps=round(eps, 3), eps_se=round(float(se), 3),
            eps_own=None if np.isnan(eps_own) else round(eps_own, 3),
            eps_prior=round(float(eps_pr), 3), origen_prior=origen_pr,
            peso_eb=round(float(w), 3), fuente=fuente,
            identificado=bool(ident), confianza=conf,
        ))

    out = pd.DataFrame(filas)
    out.to_parquet(f"{DATA}/modelo.parquet", index=False)
    # guardar meta de marcas para el reporte de validación
    pd.DataFrame([dict(marca=m, **d) for m, d in meta_marca.items()]).to_parquet(
        f"{DATA}/meta_marca.parquet", index=False)
    print(f"modelo.parquet: {len(out)} SKUs | ident={out['identificado'].sum()} "
          f"| eps medio={out['eps'].mean():.2f} | conf {out['confianza'].value_counts().to_dict()}",
          flush=True)
    return out


if __name__ == "__main__":
    estimar()
