# -*- coding: utf-8 -*-
"""Validación del motor de elasticidad — ¿el modelo funciona?

1) Diagnóstico econométrico por marca: F de instrumento, signo de ε, identificación.
2) Event-study (la prueba real): sobre los cambios de precio REALES del histórico,
   compara la elasticidad observada (Δlog unidades / Δlog precio, ventanas ±4 sem)
   contra la ε del modelo por marca. Si coinciden, la ε captura la respuesta real.
3) Estabilidad out-of-time: reestima ε por marca en las primeras 80% semanas y
   compara con la muestra completa.
4) Distribución de ε y conteo por confianza.

Imprime un reporte a stdout y lo guarda en out/validacion_elast.txt.
"""
import os

import numpy as np
import pandas as pd

from modelo import _fe_iv, _identificado

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "elast")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out")


def event_study(panel):
    """Elasticidad observada por marca a partir de cambios de precio reales."""
    obs = {}
    for aid, g in panel.sort_values("week").groupby("id_art"):
        g = g.reset_index(drop=True)
        dlp = g["log_p1"].diff()
        idx = dlp.abs().idxmax()
        if pd.isna(idx) or abs(dlp.iloc[idx]) < 0.02:
            continue
        antes = g.iloc[max(0, idx - 4):idx]
        despues = g.iloc[idx:idx + 4]
        if (antes["piezas"].fillna(0).sum() <= 0) or (despues["piezas"].fillna(0).sum() <= 0):
            continue
        du = np.log1p(despues["unidades"].mean()) - np.log1p(antes["unidades"].mean())
        dp = g["log_p1"].iloc[idx] - g["log_p1"].iloc[max(0, idx - 1)]
        if abs(dp) < 0.02:
            continue
        obs.setdefault(g["marca"].iloc[0], []).append(du / dp)
    return {m: float(np.median(v)) for m, v in obs.items() if len(v) >= 5}


def estabilidad_oot(panel):
    """ε por marca en train (80% semanas iniciales) vs completo."""
    semanas = np.sort(panel["week"].unique())
    corte = semanas[int(len(semanas) * 0.8)]
    train = panel[panel["week"] < corte]
    res = {}
    for marca, sub in train.groupby("marca"):
        r = _fe_iv(sub)
        if r and _identificado(r):
            res[marca] = r["eps"]
    return res


def validar():
    panel = pd.read_parquet(f"{DATA}/panel.parquet")
    modelo = pd.read_parquet(f"{DATA}/modelo.parquet")
    meta = pd.read_parquet(f"{DATA}/meta_marca.parquet")

    eps_marca = {r.marca: r.eps_raw for r in meta.itertuples() if r.ident}
    obs = event_study(panel)
    oot = estabilidad_oot(panel)

    lin = ["=" * 64, "VALIDACIÓN — MOTOR DE ELASTICIDAD", "=" * 64,
           "", "1) Diagnóstico por marca (ε<0 significativo |t|≥1.64; IV si F≥10, si no OLS)"]
    lin.append(f"{'marca':22s} {'eps':>7s} {'se':>6s} {'F':>8s} {'n':>8s}  estado")
    for r in meta.sort_values("F", ascending=False).itertuples():
        estado = "OK" if r.ident else "DÉBIL/NO-IDENT"
        eps = f"{r.eps_raw:+.2f}" if pd.notna(r.eps_raw) else "  n/a"
        se = f"{r.se:.2f}" if pd.notna(r.se) else " n/a"
        F = f"{r.F:.1f}" if pd.notna(r.F) else "  n/a"
        lin.append(f"{str(r.marca)[:22]:22s} {eps:>7s} {se:>6s} {F:>8s} {int(r.n):>8d}  {estado}")

    lin += ["", "2) Event-study: ε modelo vs ε observada (cambios de precio reales)",
            f"{'marca':22s} {'modelo':>8s} {'observada':>10s}"]
    for m in sorted(set(eps_marca) | set(obs)):
        em = f"{eps_marca[m]:+.2f}" if m in eps_marca else "   n/a"
        eo = f"{obs[m]:+.2f}" if m in obs else "     n/a"
        lin.append(f"{str(m)[:22]:22s} {em:>8s} {eo:>10s}")

    lin += ["", "3) Estabilidad out-of-time (ε train 80% vs completo)",
            f"{'marca':22s} {'train':>8s} {'completo':>10s}"]
    for m in sorted(set(oot) | set(eps_marca)):
        et = f"{oot[m]:+.2f}" if m in oot else "   n/a"
        ec = f"{eps_marca[m]:+.2f}" if m in eps_marca else "     n/a"
        lin.append(f"{str(m)[:22]:22s} {et:>8s} {ec:>10s}")

    ident = modelo["identificado"].sum()
    elast = (modelo["eps"] < -1).sum()
    lin += ["", "4) Resumen SKU",
            f"  SKUs: {len(modelo)} | identificados: {ident} ({ident/len(modelo):.0%})",
            f"  elásticos (|ε|>1): {elast} ({elast/len(modelo):.0%})",
            f"  ε medio: {modelo['eps'].mean():.2f}  mediana: {modelo['eps'].median():.2f}",
            f"  confianza: {modelo['confianza'].value_counts().to_dict()}"]

    txt = "\n".join(lin)
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/validacion_elast.txt", "w") as f:
        f.write(txt)
    print(txt, flush=True)


if __name__ == "__main__":
    validar()
