# -*- coding: utf-8 -*-
"""T2.1 — Carrera de pronosticadores para la COLA LARGA (intermitente/grumosa).

Regla de la casa (FVA): un método solo entra si le GANA a la base actual
(media_4s) en validación out-of-time. Métrica principal: RMSSE (el WAPE en
series intermitentes premia pronosticar cero — advertencia #3 del benchmark);
WAPE agregado solo como referencia.

Métodos (implementación directa, sin dependencias nuevas):
  - media_4s   : media de las últimas 4 semanas del train (base actual)
  - croston    : SES(tamaños) / SES(intervalos), α=0.1
  - sba        : Croston × (1 − α/2)  (corrección de sesgo Syntetos-Boylan)
  - tsb        : prob. de demanda actualizada CADA semana (maneja obsolescencia)

Holdout: últimas H=3 semanas (mismo que validar.py). Segmentado por clase de
serie (data/adi_cv2.parquet). Salida: data/carrera_intermitentes.parquet.
"""
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
H = 3
ALFA = 0.1


def _croston(y, alfa=ALFA, sba=False):
    """Croston/SBA sobre una serie 1-D (train). Devuelve pronóstico por semana."""
    nz = np.flatnonzero(y > 0)
    if len(nz) == 0:
        return 0.0
    z = y[nz][0]                       # tamaño suavizado
    p = nz[0] + 1.0                    # intervalo suavizado
    prev = nz[0]
    for i in nz[1:]:
        z = alfa * y[i] + (1 - alfa) * z
        p = alfa * (i - prev) + (1 - alfa) * p
        prev = i
    f = z / max(p, 1e-9)
    return f * (1 - alfa / 2) if sba else f


def _tsb(y, alfa=ALFA, beta=0.1):
    """TSB: prob. de demanda se actualiza cada periodo (baja si no hay venta)."""
    if (y > 0).sum() == 0:
        return 0.0
    d = float(y[0] > 0)
    z = y[0] if y[0] > 0 else y[y > 0][0]
    for v in y[1:]:
        if v > 0:
            d = beta * 1 + (1 - beta) * d
            z = alfa * v + (1 - alfa) * z
        else:
            d = beta * 0 + (1 - beta) * d
    return d * z


def correr():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan["unidades"] = pan.unidades_rec.astype(float)
    clase = pd.read_parquet(os.path.join(DATA, "adi_cv2.parquet")).set_index("codigo").clase
    sem = np.sort(pan.semana.unique())
    train_fin, hold = sem[-H - 1], sem[-H:]
    u = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                         aggfunc="sum").reindex(columns=sem))
    # solo SKUs con historia en train y presencia en el motor (activos)
    act = pan[pan.activo].codigo.unique()
    u = u.loc[u.index.isin(act)]

    res = []
    for cod, row in u.iterrows():
        tr = row.loc[:train_fin]
        first = tr.first_valid_index()
        if first is None:
            continue
        y = tr.loc[first:].fillna(0.0).values      # span activo, ceros incluidos
        if len(y) < 8:
            continue
        real = row.loc[hold].fillna(0.0).mean()
        # escala del RMSSE: RMSE del naive-1 en el train (estándar M5)
        dif = np.diff(y)
        escala = np.sqrt(np.mean(dif ** 2)) if len(dif) and (dif != 0).any() else np.nan
        preds = {
            "media_4s": y[-4:].mean(),
            "croston": _croston(y),
            "sba": _croston(y, sba=True),
            "tsb": _tsb(y),
        }
        for met, p in preds.items():
            res.append({"codigo": cod, "metodo": met, "pred": p, "real": real,
                        "escala": escala, "clase": clase.get(cod, "sin clase")})
    df = pd.DataFrame(res)
    df["rmsse"] = np.abs(df.pred - df.real) / df.escala   # H=3 promediado: |err|/escala
    df.to_parquet(os.path.join(DATA, "carrera_intermitentes.parquet"), index=False)

    print(f"== CARRERA DE COLA LARGA (holdout {pd.Timestamp(hold[0]).date()} → "
          f"{pd.Timestamp(hold[-1]).date()}, {df.codigo.nunique():,} SKUs) ==", flush=True)
    for cl in ["grumosa (lumpy)", "intermitente", "errática", "suave"]:
        s = df[df.clase == cl]
        if s.empty:
            continue
        print(f"\n  {cl} ({s.codigo.nunique():,} SKUs):", flush=True)
        base = s[s.metodo == "media_4s"]
        wape_b = (base.pred - base.real).abs().sum() / max(base.real.sum(), 1e-9)
        rm_b = base.rmsse.replace([np.inf], np.nan).dropna().mean()
        for met in ["media_4s", "croston", "sba", "tsb"]:
            m = s[s.metodo == met]
            wape = (m.pred - m.real).abs().sum() / max(m.real.sum(), 1e-9)
            rm = m.rmsse.replace([np.inf], np.nan).dropna().mean()
            skill = 100 * (1 - rm / rm_b) if rm_b else np.nan
            gana = "✓ GANA" if (met != "media_4s" and rm < rm_b) else ""
            print(f"    {met:<9} RMSSE {rm:.3f} (skill {skill:+.1f}%) | WAPE {wape:.3f} {gana}",
                  flush=True)
        # trampa del cero: ¿cuánto WAPE tendría pronosticar 0?
        w0 = base.real.abs().sum() / max(base.real.sum(), 1e-9)
        pred0_rmsse = (base.real / base.escala).replace([np.inf], np.nan).dropna().mean()
        print(f"    (cero)    RMSSE {pred0_rmsse:.3f} | WAPE {w0:.3f}  ← la 'trampa del cero'",
              flush=True)


if __name__ == "__main__":
    correr()
