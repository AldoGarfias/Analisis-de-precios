# -*- coding: utf-8 -*-
"""Retador ML del pronóstico base u0: gradient boosting vs la media simple.

Regla del proyecto: un modelo de pronóstico SOLO entra si le gana a la media
simple en backtest out-of-time. Este script corre esa competencia en el mismo
holdout de 3 semanas (el horizonte de decisión) que usa validar.py.

Diseño anti-fuga:
  - Muestras de entrenamiento: cortes históricos t; features SOLO con datos ≤ t;
    target = venta recurrente promedio de las 3 semanas siguientes (t+1..t+3).
  - Solo cortes cuyo target termina ANTES del holdout final.
  - Evaluación: el mismo último corte que validar.py (train 99 sem → 3 sem).

Modelo: HistGradientBoosting con pérdida Poisson (equivalente a XGBoost para
este caso; convención del repo — XGBoost daría lo mismo si el equipo lo prefiere).
"""
import os

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
H = 3  # horizonte (semanas) = ciclo de decisión


def _features(m, p, t, semanas):
    """Features por SKU con datos hasta la semana índice t (incluida)."""
    hist = m[:, : t + 1]
    f = {
        "ult": hist[:, -1],
        "media4": hist[:, -4:].mean(axis=1),
        "media8": hist[:, -8:].mean(axis=1),
        "media12": hist[:, -12:].mean(axis=1),
        "std4": hist[:, -4:].std(axis=1),
        # ratios ACOTADOS: sin clip, x/~0 dispara el log-link Poisson al infinito
        "tendencia": np.clip(hist[:, -4:].mean(axis=1)
                             / np.maximum(hist[:, -12:].mean(axis=1), 0.5), 0, 10),
        "sem_activas": (hist > 0).sum(axis=1),
        "log_precio": np.log1p(np.nan_to_num(p[:, t], nan=0.0)),
        "cambio_precio_4s": np.clip(np.nan_to_num(
            p[:, t] / np.maximum(p[:, max(0, t - 4)], 1e-9) - 1, nan=0.0), -0.5, 0.5),
        "mes": np.full(m.shape[0], pd.Timestamp(semanas[t]).month),
        "sem_anio": np.full(m.shape[0], pd.Timestamp(semanas[t]).weekofyear),
    }
    return pd.DataFrame(f)


def correr():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
    semanas = np.sort(pan.semana.unique())
    mdf = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                           aggfunc="sum").reindex(columns=semanas).fillna(0.0))
    pdf = (pan.pivot_table(index="codigo", columns="semana", values="precio_lista",
                           aggfunc="last").reindex(columns=semanas).ffill(axis=1))  # solo ffill: bfill filtraría precios del futuro
    m, p = mdf.values, pdf.values
    n_sem = m.shape[1]
    corte_final = n_sem - H - 1          # última semana visible para el holdout

    # entrenamiento: cortes cada 2 semanas, targets que terminan antes del holdout
    cortes_train = [t for t in range(16, corte_final - H, 2)]
    X_tr, y_tr = [], []
    for t in cortes_train:
        X_tr.append(_features(m, p, t, semanas))
        y_tr.append(m[:, t + 1: t + 1 + H].mean(axis=1))
    X_tr = pd.concat(X_tr, ignore_index=True)
    y_tr = np.concatenate(y_tr)
    print(f"train: {len(X_tr):,} muestras ({len(cortes_train)} cortes × {m.shape[0]:,} SKUs)",
          flush=True)

    # Formulación RESIDUAL (estable): el GBM aprende el factor de corrección
    # sobre la media_4s, acotado a [0, 5]. Evita que un link exponencial
    # extrapole niveles absurdos; si el modelo no aporta, factor ≈ 1 = campeona.
    base_tr = X_tr["media4"].values
    ratio_tr = np.clip(y_tr / np.maximum(base_tr, 0.5), 0.0, 5.0)
    modelo = HistGradientBoostingRegressor(loss="squared_error", max_iter=300,
                                           learning_rate=0.08, max_depth=6,
                                           random_state=7)
    modelo.fit(X_tr, ratio_tr)

    # evaluación en el MISMO holdout que validar.py
    X_te = _features(m, p, corte_final, semanas)
    y_te = m[:, corte_final + 1: corte_final + 1 + H].mean(axis=1)
    pred_m4 = m[:, corte_final - 3: corte_final + 1].mean(axis=1)
    factor = np.clip(modelo.predict(X_te), 0.0, 5.0)
    pred_ml = np.maximum(pred_m4, 0.0) * factor

    def wape(pred):
        return np.abs(pred - y_te).sum() / y_te.sum()

    def sesgo(pred):
        return (pred - y_te).sum() / y_te.sum()

    print(f"\n== COMPETENCIA en holdout {pd.Timestamp(semanas[corte_final+1]).date()} "
          f"→ {pd.Timestamp(semanas[corte_final+H]).date()} ({m.shape[0]:,} SKUs) ==", flush=True)
    print(f"  media_4s (campeona actual): WAPE {wape(pred_m4):.3f}  sesgo {sesgo(pred_m4):+.3f}", flush=True)
    print(f"  GBM (lags+tendencia+mes):   WAPE {wape(pred_ml):.3f}  sesgo {sesgo(pred_ml):+.3f}", flush=True)
    mejora = 100 * (wape(pred_m4) - wape(pred_ml)) / wape(pred_m4)
    print(f"  → {'GANA GBM' if mejora > 0 else 'GANA media_4s'} "
          f"({mejora:+.1f}% de mejora en WAPE)", flush=True)
    # por tercil de volumen (¿dónde ayuda?)
    terc = pd.qcut(pred_m4, 3, labels=["bajo", "medio", "alto"])
    for tt in ["bajo", "medio", "alto"]:
        mask = np.asarray(terc == tt)
        w4 = np.abs(pred_m4[mask] - y_te[mask]).sum() / max(y_te[mask].sum(), 1e-9)
        wm = np.abs(pred_ml[mask] - y_te[mask]).sum() / max(y_te[mask].sum(), 1e-9)
        print(f"    tercil {tt:<6}: media_4s {w4:.3f} vs GBM {wm:.3f}", flush=True)


def robustez(n_ventanas=4, paso=3):
    """Validación RODANTE: la competencia en varias ventanas, re-entrenando sin
    fuga en cada corte. El GBM solo destrona a la media si gana consistentemente."""
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
    semanas = np.sort(pan.semana.unique())
    m = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                         aggfunc="sum").reindex(columns=semanas).fillna(0.0)).values
    p = (pan.pivot_table(index="codigo", columns="semana", values="precio_lista",
                         aggfunc="last").reindex(columns=semanas).ffill(axis=1)).values
    n_sem = m.shape[1]
    print(f"\n== ROBUSTEZ: {n_ventanas} ventanas rodantes ==", flush=True)
    ganadas = 0
    for k in range(n_ventanas):
        corte = n_sem - H - 1 - k * paso
        cortes_train = [t for t in range(16, corte - H, 2)]
        X_tr = pd.concat([_features(m, p, t, semanas) for t in cortes_train],
                         ignore_index=True)
        y_tr = np.concatenate([m[:, t + 1: t + 1 + H].mean(axis=1) for t in cortes_train])
        ratio_tr = np.clip(y_tr / np.maximum(X_tr["media4"].values, 0.5), 0.0, 5.0)
        mod = HistGradientBoostingRegressor(loss="squared_error", max_iter=300,
                                            learning_rate=0.08, max_depth=6, random_state=7)
        mod.fit(X_tr, ratio_tr)
        X_te = _features(m, p, corte, semanas)
        y_te = m[:, corte + 1: corte + 1 + H].mean(axis=1)
        pm4 = m[:, corte - 3: corte + 1].mean(axis=1)
        pml = np.maximum(pm4, 0.0) * np.clip(mod.predict(X_te), 0.0, 5.0)
        w4 = np.abs(pm4 - y_te).sum() / y_te.sum()
        wm = np.abs(pml - y_te).sum() / y_te.sum()
        gano = wm < w4
        ganadas += gano
        print(f"  corte {pd.Timestamp(semanas[corte]).date()}: media_4s {w4:.3f} vs "
              f"GBM {wm:.3f}  {'GBM ✓' if gano else 'media_4s ✓'}", flush=True)
    print(f"  → GBM gana {ganadas}/{n_ventanas} ventanas", flush=True)


def _sba_fila(y, alfa=0.1):
    """SBA (Croston con corrección Syntetos-Boylan) sobre una serie hasta hoy."""
    nz = np.flatnonzero(y > 0)
    if len(nz) == 0:
        return 0.0
    y = y[nz[0]:]
    nz = np.flatnonzero(y > 0)
    z, p, prev = y[nz[0]], float(nz[0] + 1), nz[0]
    for i in nz[1:]:
        z = alfa * y[i] + (1 - alfa) * z
        p = alfa * (i - prev) + (1 - alfa) * p
        prev = i
    return (z / max(p, 1e-9)) * (1 - alfa / 2)


def _sba_por_corte(m, corte):
    return np.array([_sba_fila(m[i, :corte + 1]) for i in range(m.shape[0])])


def _cargar_matrices():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
    semanas = np.sort(pan.semana.unique())
    mdf = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                           aggfunc="sum").reindex(columns=semanas).fillna(0.0))
    p = (pan.pivot_table(index="codigo", columns="semana", values="precio_lista",
                         aggfunc="last").reindex(columns=semanas).ffill(axis=1)).values
    return mdf, p, semanas


def _fit(m, p, semanas, corte):
    """Entrena el GBM residual con cortes hasta `corte` (sin fuga)."""
    cortes_train = [t for t in range(16, corte - H, 2)]
    X_tr = pd.concat([_features(m, p, t, semanas) for t in cortes_train],
                     ignore_index=True)
    y_tr = np.concatenate([m[:, t + 1: t + 1 + H].mean(axis=1) for t in cortes_train])
    ratio = np.clip(y_tr / np.maximum(X_tr["media4"].values, 0.5), 0.0, 5.0)
    mod = HistGradientBoostingRegressor(loss="squared_error", max_iter=300,
                                        learning_rate=0.08, max_depth=6, random_state=7)
    mod.fit(X_tr, ratio)
    return mod


def _predice(mod, m, p, semanas, corte):
    X_te = _features(m, p, corte, semanas)
    base = m[:, corte - 3: corte + 1].mean(axis=1)
    return np.maximum(base, 0.0) * np.clip(mod.predict(X_te), 0.0, 5.0)


def generar_u0():
    """Genera el u0 oficial del pipeline con el GBM residual (campeón del
    backtest 4/4) y sus BANDAS de error propias, medidas fuera de muestra.

    Salidas:
      data/forecast_u0.parquet     — codigo, u0 (unid/sem, venta recurrente,
                                     horizonte = ciclo de 3 semanas)
      data/forecast_bandas.parquet — p10/p50/p90 del ratio real/pred por tercil
                                     de volumen (mismas columnas que backtest.parquet)
    """
    mdf, p, semanas = _cargar_matrices()
    m = mdf.values
    n_sem = m.shape[1]
    # clase de serie (T2.1 aprobado): SBA compite contra el GBM por clase; solo
    # sustituye si gana ≥3 de 4 ventanas OOT (regla FVA, con RMSSE — el WAPE en
    # intermitentes premia pronosticar cero)
    ruta_c = os.path.join(DATA, "adi_cv2.parquet")
    clase = (pd.read_parquet(ruta_c).set_index("codigo").clase
             .reindex(mdf.index).fillna("sin clase").values
             if os.path.exists(ruta_c) else np.full(len(mdf), "sin clase"))
    wins = {}

    # 1) bandas FUERA DE MUESTRA: 4 ventanas rodantes, re-entrenando en cada una
    regs = []
    for k in range(4):
        corte = n_sem - H - 1 - k * 3
        mod = _fit(m, p, semanas, corte)
        pred = _predice(mod, m, p, semanas, corte)
        pred_sba = _sba_por_corte(m, corte)
        real = m[:, corte + 1: corte + 1 + H].mean(axis=1)
        dif = np.diff(m[:, :corte + 1], axis=1)
        esc = np.sqrt((dif ** 2).mean(axis=1))
        esc[esc == 0] = np.nan
        for cl in np.unique(clase):
            msk = clase == cl
            r_g = np.nanmean(np.abs(pred[msk] - real[msk]) / esc[msk])
            r_s = np.nanmean(np.abs(pred_sba[msk] - real[msk]) / esc[msk])
            wins[cl] = wins.get(cl, 0) + int(r_s < r_g)
        regs.append(pd.DataFrame({"pred": pred, "pred_sba": pred_sba, "real": real,
                                  "clase": clase}))
        print(f"  banda {k+1}/4 (corte {pd.Timestamp(semanas[corte]).date()}): "
              f"WAPE GBM {np.abs(pred-real).sum()/real.sum():.3f}", flush=True)
    usa_sba = {cl for cl, w in wins.items() if w >= 3}
    print(f"  duelo por clase (ventanas ganadas por SBA de 4): "
          f"{ {k: v for k, v in sorted(wins.items())} } → SBA sustituye en: "
          f"{sorted(usa_sba) or 'ninguna'}", flush=True)
    ev = pd.concat(regs, ignore_index=True)
    ev["pred"] = np.where(ev.clase.isin(usa_sba), ev.pred_sba, ev.pred)
    ev = ev[ev.pred > 0]
    ev["ratio"] = ev.real / ev.pred
    ev["tercil"] = pd.qcut(ev.pred, 3, labels=["bajo", "medio", "alto"])
    bandas = ev.groupby("tercil", observed=True).ratio.quantile([.1, .5, .9]).unstack()
    bandas.columns = ["p10", "p50", "p90"]
    out_b = bandas.reset_index()
    out_b["metodo_u0"] = "gbm_residual"
    out_b["wape"] = float(np.abs(ev.pred - ev.real).sum() / ev.real.sum())
    out_b["sesgo"] = float((ev.pred - ev.real).sum() / ev.real.sum())
    out_b.to_parquet(os.path.join(DATA, "forecast_bandas.parquet"), index=False)
    print("  bandas GBM por tercil:", flush=True)
    print(bandas.round(2).to_string(), flush=True)

    # 2) modelo FINAL con toda la historia; u0 = predicción desde la última semana
    # (GBM residual; SBA en las clases donde ganó el duelo OOT)
    mod = _fit(m, p, semanas, n_sem - 1)
    u0 = _predice(mod, m, p, semanas, n_sem - 1)
    if usa_sba:
        u0_sba = _sba_por_corte(m, n_sem - 1)
        sw = np.isin(clase, list(usa_sba))
        u0 = np.where(sw, u0_sba, u0)
        print(f"  u0 final: SBA en {int(sw.sum()):,} SKUs "
              f"({sorted(usa_sba)}), GBM en el resto", flush=True)
    # T2.2 (2026-07-27): bandas CONDICIONALES por SKU (GBM cuantil α=0.1/0.9)
    # SOLO en las clases donde ganaron el examen de cobertura OOT (suave: misma
    # cobertura con banda 44% más angosta; lumpy: mejor cobertura). Errática e
    # intermitente conservan las bandas por tercil.
    CLASES_CUANTIL = ["suave", "grumosa (lumpy)"]
    cortes_train = [t for t in range(16, n_sem - 1 - H, 2)]
    X_tr = pd.concat([_features(m, p, t, semanas) for t in cortes_train],
                     ignore_index=True)
    y_tr = np.concatenate([m[:, t + 1: t + 1 + H].mean(axis=1) for t in cortes_train])
    X_hoy = _features(m, p, n_sem - 1, semanas)
    bq = {}
    for alfa in (0.10, 0.90):
        mq = HistGradientBoostingRegressor(loss="quantile", quantile=alfa, max_iter=250,
                                           learning_rate=0.08, max_depth=6, random_state=7)
        mq.fit(X_tr, y_tr)
        bq[alfa] = np.maximum(mq.predict(X_hoy), 0.0)
    en_q = np.isin(clase, CLASES_CUANTIL)
    u_p10 = np.where(en_q, np.minimum(bq[0.10], u0), np.nan)
    u_p90 = np.where(en_q, np.maximum(bq[0.90], u0), np.nan)
    print(f"  bandas por SKU (cuantil condicional): {int(en_q.sum()):,} SKUs "
          f"({CLASES_CUANTIL}); el resto usa tercil", flush=True)
    pd.DataFrame({"codigo": mdf.index, "u0": u0, "u_p10": u_p10, "u_p90": u_p90,
                  "metodo": np.where(np.isin(clase, list(usa_sba)), "sba",
                                     "gbm_residual")}).to_parquet(
        os.path.join(DATA, "forecast_u0.parquet"), index=False)
    print(f"guardado data/forecast_u0.parquet ({len(u0):,} SKUs) y "
          f"data/forecast_bandas.parquet", flush=True)


def hibrido(n_sel=3, paso=3):
    """Competencia de HÍBRIDOS en el holdout final:

      - media_4s          (campeona simple)
      - GBM residual      (campeona ML)
      - híbrido-selección: cada SKU usa el modelo que MENOS error acumuló en
                          las n_sel ventanas ANTERIORES (sin fuga: la elección
                          nunca ve el holdout final)
      - híbrido-promedio: 0.5·media_4s + 0.5·GBM (lección M5: las combinaciones
                          simples son difíciles de vencer)
    """
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
    semanas = np.sort(pan.semana.unique())
    mdf = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                           aggfunc="sum").reindex(columns=semanas).fillna(0.0))
    m = mdf.values
    p = (pan.pivot_table(index="codigo", columns="semana", values="precio_lista",
                         aggfunc="last").reindex(columns=semanas).ffill(axis=1)).values
    n_sem = m.shape[1]

    def _entrena_y_predice(corte):
        cortes_train = [t for t in range(16, corte - H, 2)]
        X_tr = pd.concat([_features(m, p, t, semanas) for t in cortes_train],
                         ignore_index=True)
        y_tr = np.concatenate([m[:, t + 1: t + 1 + H].mean(axis=1) for t in cortes_train])
        ratio = np.clip(y_tr / np.maximum(X_tr["media4"].values, 0.5), 0.0, 5.0)
        mod = HistGradientBoostingRegressor(loss="squared_error", max_iter=300,
                                            learning_rate=0.08, max_depth=6, random_state=7)
        mod.fit(X_tr, ratio)
        X_te = _features(m, p, corte, semanas)
        y_te = m[:, corte + 1: corte + 1 + H].mean(axis=1)
        pm4 = m[:, corte - 3: corte + 1].mean(axis=1)
        pml = np.maximum(pm4, 0.0) * np.clip(mod.predict(X_te), 0.0, 5.0)
        return y_te, pm4, pml

    # 1) acumular error por SKU en las ventanas de SELECCIÓN (anteriores al final)
    err_m4 = np.zeros(m.shape[0])
    err_ml = np.zeros(m.shape[0])
    for k in range(1, n_sel + 1):
        corte = n_sem - H - 1 - k * paso
        y, pm4, pml = _entrena_y_predice(corte)
        err_m4 += np.abs(pm4 - y)
        err_ml += np.abs(pml - y)
    usa_ml = err_ml < err_m4          # elección POR SKU con historia previa

    # 2) evaluación de los 4 contendientes en el holdout FINAL
    corte = n_sem - H - 1
    y, pm4, pml = _entrena_y_predice(corte)
    p_sw = np.where(usa_ml, pml, pm4)
    p_en = 0.5 * pm4 + 0.5 * pml

    def wape(pred):
        return np.abs(pred - y).sum() / y.sum()

    print(f"\n== HÍBRIDOS en holdout final ({m.shape[0]:,} SKUs; selección con "
          f"{n_sel} ventanas previas) ==", flush=True)
    print(f"  media_4s          : WAPE {wape(pm4):.3f}", flush=True)
    print(f"  GBM               : WAPE {wape(pml):.3f}", flush=True)
    print(f"  híbrido-selección : WAPE {wape(p_sw):.3f}  "
          f"({usa_ml.sum():,} SKUs usan GBM, {(~usa_ml).sum():,} media_4s)", flush=True)
    print(f"  híbrido-promedio  : WAPE {wape(p_en):.3f}", flush=True)
    mejor = min([("media_4s", wape(pm4)), ("GBM", wape(pml)),
                 ("híbrido-selección", wape(p_sw)), ("híbrido-promedio", wape(p_en))],
                key=lambda x: x[1])
    print(f"  → MEJOR: {mejor[0]} (WAPE {mejor[1]:.3f})", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "competencia":
        correr()      # la carrera completa vs media_4s + robustez + híbridos
        robustez()
        hibrido()
    else:
        generar_u0()  # modo pipeline: genera u0 oficial + bandas
