# -*- coding: utf-8 -*-
"""FORECAST MENSUAL PROPIO (tercera voz) + EXAMEN MENSUAL DE LAS TRES FUENTES.

Usuario 2026-07-31: "¿no deberíamos implementar un forecast alternativo de
ventas? para poder tener 3 comparativas e ir retroalimentando su error, mes
con mes".

Las voces (target común: venta TOTAL mensual por SKU, la que rota stock):
  1. AWS            — ML del negocio (mensual, P60, caja negra).
  2. MOTOR (u0)     — GBM/SBA semanal mensualizado ×4.345, archivado EX-ANTE
                      en el pred_* del mes (pronostica RECURRENTE; se califica
                      con esa etiqueta, no es cabeza a cabeza puro).
  3. PROPIO (ADOPTADO 2026-07-31 por duelo OOT, 6/6 al ingenuo, WAPE 0.394
     vs 0.432) — ENSAMBLE 50/50: GBM residual mensual (lags, nivel,
     intermitencia, mes, tendencia; aprende el factor sobre la base
     0.6·mes_anterior+0.4·mediana_3m) + ingenuo. Alimenta meses_stock v3.
     (El ESTACIONAL simple perdió su duelo — 3/6 y 2/6 — y quedó fuera;
     revancha cuando el backfill 2016+ dé años de estacionalidad.)
  Pisos permanentes: INGENUO (mes anterior) e INGENUO ESTACIONAL (año pasado).
  Regla de la casa (FVA): nada entra sin ganarle al piso; JUEZ = media
  geométrica de ratios + mediana (<1 ambas), meses anómalos winsorizados
  (ver _veredicto y docs/EVALUACION_FORECAST_MENSUAL.md).

Modos:
  backtest  — valida el propio: predice cada uno de los últimos 6 meses usando
              solo datos anteriores; WAPE vs ingenuo. Si no gana, NO entra.
  generar   — archiva INMUTABLE la predicción de los próximos 5 meses
              (data/forecast_mensual_propio/pred_YYYYMM.parquet).
  examen    — al cierre de cada mes: califica lo que cada fuente ARCHIVÓ para
              ese mes vs la venta real → out/examen_forecasts.csv (acumulado,
              el marcador mes con mes) + impresión del ranking.

Cron: en la cadena diaria, los primeros días de cada mes corre examen del mes
recién cerrado + generar del nuevo (seguimiento_frenos).
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DIR_P = os.path.join(DATA, "forecast_mensual_propio")
HORIZONTE = 5
K_ESTACION = 1.0   # encogimiento del índice estacional: w = años/(años+K)


def _panel_cubre(mes):
    """FRESCURA (auditoría 2026-07-31, N3 — el análogo de C6): True si el panel
    cubre COMPLETO el mes dado. Sin esto, examen/generar usarían un mes parcial
    y el error quedaría sellado en archivos inmutables (y meses_stock v3
    heredaría demanda subestimada = falsos sobrestock)."""
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"), columns=["semana"])
    fin_cubierto = pd.Timestamp(pan.semana.max()) + pd.Timedelta(days=6)
    fin_mes = pd.Period(mes, freq="M").end_time.normalize()
    return fin_cubierto >= fin_mes


def _mensual():
    """Venta TOTAL mensual por SKU + fracción de semanas sin stock por mes."""
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    if "activo" in pan.columns:
        pan = pan[pan.activo]
    col_u = "unidades"   # panel.unidades = TOTAL (escenarios lo sobrescribe, aquí no)
    pan["_mes"] = pd.to_datetime(pan.semana).dt.to_period("M")
    vm = pan.groupby(["codigo", "_mes"])[col_u].sum().unstack().fillna(0.0)
    ss = None
    ruta_ex = os.path.join(DATA, "reporte61", "existencias_sem.parquet")
    if os.path.exists(ruta_ex):
        ex = pd.read_parquet(ruta_ex)
        col = "disp_venta" if "disp_venta" in ex.columns else "disponible"
        ex["_mes"] = pd.to_datetime(ex.semana).dt.to_period("M")
        ss = (ex.assign(_sin=lambda d: d[col] <= 0)
              .groupby(["codigo", "_mes"])._sin.mean().unstack())
    return vm, ss


def _predice(vm, ss, hasta_mes, horizonte=HORIZONTE):
    """Predicción propia para los `horizonte` meses posteriores a `hasta_mes`,
    usando SOLO columnas ≤ hasta_mes."""
    hist = vm.loc[:, vm.columns <= hasta_mes]
    if hist.shape[1] < 4:
        return None
    # base: INGENUO ESTACIONAL (v2 tras el backtest 2026-07-31: la mediana de
    # 3 meses perdió 3/6 vs el ingenuo — lenta contra tendencia). Base = mezcla
    # del mes anterior (ágil) con la mediana de 3 (estable), sin meses de
    # cero-por-stockout; luego se re-estacionaliza: ×idx(mes_obj)/idx(mes_base)
    ult3 = hist.iloc[:, -3:]
    med3 = ult3.median(axis=1)
    if ss is not None:
        s3 = ss.reindex(index=hist.index, columns=ult3.columns).fillna(0.0)
        valida = ~((ult3 <= 1) & (s3 >= 0.5))
        med3 = ult3.where(valida).median(axis=1).fillna(0.0)
        med3[valida.sum(axis=1) == 0] = 0.0
    base = 0.6 * hist.iloc[:, -1] + 0.4 * med3
    # índice estacional por mes calendario (global del SKU, encogido)
    tot = hist.mean(axis=1)
    idx_mes = {}
    for m in range(1, 13):
        cols = [c for c in hist.columns if c.month == m]
        if not cols:
            idx_mes[m] = pd.Series(1.0, index=hist.index)
            continue
        prom_m = hist[cols].mean(axis=1)
        crudo = (prom_m / tot.replace(0, np.nan)).fillna(1.0).clip(0.25, 4.0)
        w = len(cols) / (len(cols) + K_ESTACION)
        idx_mes[m] = 1 + w * (crudo - 1)
    filas = []
    idx_base = idx_mes[hasta_mes.month]
    for h in range(1, horizonte + 1):
        mes_obj = hasta_mes + h
        pred = (base * idx_mes[mes_obj.month] / idx_base.replace(0, 1)).clip(lower=0)
        filas.append(pd.DataFrame({"codigo": pred.index, "mes": str(mes_obj),
                                   "pred": pred.values.round(2)}))
    return pd.concat(filas, ignore_index=True)


def _features_ml(vm, hasta_mes):
    """Matriz (SKU, mes_objetivo) para el GBM residual mensual: predice el
    FACTOR de corrección sobre la base (la formulación que ganó en semanal)."""
    cols = [c for c in vm.columns if c <= hasta_mes]
    filas = []
    for j in range(6, len(cols)):
        m = cols[j]
        h = vm[cols[:j]]
        u1, u2, u3 = h.iloc[:, -1], h.iloc[:, -2], h.iloc[:, -3]
        med3 = h.iloc[:, -3:].median(axis=1)
        base = (0.6 * u1 + 0.4 * med3).clip(lower=0)
        nivel = h.iloc[:, -6:].mean(axis=1)
        ceros6 = (h.iloc[:, -6:] <= 0).mean(axis=1)
        df = pd.DataFrame({
            "codigo": vm.index, "mes_obj": str(m), "base": base.values,
            "l1": np.log1p(u1.values), "l2": np.log1p(u2.values),
            "l3": np.log1p(u3.values), "lnivel": np.log1p(nivel.values),
            "ceros6": ceros6.values, "mes_cal": m.month,
            "tend": (u1 / med3.replace(0, np.nan)).fillna(1.0).clip(0, 5).values,
            "y": vm[m].values if m in vm.columns else np.nan,
        })
        filas.append(df)
    return pd.concat(filas, ignore_index=True)


def backtest_ml():
    """Duelo FVA del GBM residual MENSUAL vs ingenuo (mismo juez que el
    estacional): entrena solo con meses ANTERIORES al mes de prueba."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    vm, _ = _mensual()
    vm = vm.loc[vm.sum(axis=1) > 0]
    hoy_m = pd.Timestamp.today().to_period("M")
    meses_test = sorted([hoy_m - k for k in range(1, 7)])
    todas = _features_ml(vm, hoy_m - 1)
    todas["ratio"] = (todas.y / todas.base.clip(lower=0.5)).clip(0, 5)
    print("== BACKTEST OOT del GBM residual MENSUAL (6 meses) ==", flush=True)
    res = []
    for mt in meses_test:
        tr = todas[(todas.mes_obj < str(mt)) & todas.y.notna()]
        te = todas[todas.mes_obj == str(mt)].copy()
        if len(tr) < 5000 or te.empty:
            continue
        Xc = ["l1", "l2", "l3", "lnivel", "ceros6", "mes_cal", "tend"]
        gbm = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.06,
                                            max_depth=6, random_state=7)
        gbm.fit(tr[Xc], tr.ratio)
        te["pred"] = (gbm.predict(te[Xc]).clip(0, 5) * te.base).clip(lower=0)
        real = te.set_index("codigo").y
        comunes = real[real > 0].index
        p = te.set_index("codigo").pred
        ing = vm[mt - 1] if (mt - 1) in vm.columns else None
        wape_m = float((p.reindex(comunes) - real[comunes]).abs().sum() / real[comunes].sum())
        wape_i = float((ing.reindex(comunes).fillna(0) - real[comunes]).abs().sum()
                       / real[comunes].sum())
        res.append((str(mt), wape_m, wape_i))
        print(f"  {mt}: WAPE GBM {wape_m:.3f} vs ingenuo {wape_i:.3f} "
              f"{'✓' if wape_m < wape_i else '✗'} ({len(comunes):,} SKUs)", flush=True)
    return _veredicto(res, "GBM mensual")


def backtest():
    vm, ss = _mensual()
    hoy_m = pd.Timestamp.today().to_period("M")
    meses_test = [hoy_m - k for k in range(1, 7)]
    print("== BACKTEST OOT del forecast mensual propio (6 meses) ==", flush=True)
    res = []
    for mt in sorted(meses_test):
        pr = _predice(vm, ss, mt - 1, horizonte=1)
        if pr is None or str(mt) not in set(pr.mes):
            continue
        p = pr[pr.mes == str(mt)].set_index("codigo").pred
        real = vm[mt] if mt in vm.columns else None
        if real is None:
            continue
        ing = vm[mt - 1] if (mt - 1) in vm.columns else None
        comunes = real[real > 0].index.intersection(p.index)
        wape_p = float((p.reindex(comunes) - real[comunes]).abs().sum() / real[comunes].sum())
        wape_i = float((ing.reindex(comunes).fillna(0) - real[comunes]).abs().sum()
                       / real[comunes].sum())
        res.append((str(mt), wape_p, wape_i))
        print(f"  {mt}: WAPE propio {wape_p:.3f} vs ingenuo {wape_i:.3f} "
              f"{'✓' if wape_p < wape_i else '✗'} ({len(comunes):,} SKUs)", flush=True)
    return _veredicto(res, "estacional")


def _veredicto(res, nombre):
    """JUEZ (codificado 2026-07-31 tras la evaluación exhaustiva — la vara
    'ganar 5 de 6' era estadísticamente ciega con n=6): media GEOMÉTRICA de
    los ratios WAPE_retador/WAPE_campeón (AvgRelMAE de Davydenko-Fildes) y
    mediana como desempate; ENTRA si ambas < 1. Meses anómalos declarados
    ex-ante (2026-06 = venta de aniversario) se winsorizan al segundo peor
    ratio — se reportan, no se excluyen en silencio."""
    ANOMALOS = {"2026-06"}
    ratios = {m: a / b for m, a, b in res if b > 0}
    if not ratios:
        return False
    rs = pd.Series(ratios)
    normales = rs[~rs.index.isin(ANOMALOS)]
    if len(normales) >= 2 and any(m in rs.index for m in ANOMALOS):
        # segundo peor ratio NORMAL (ronda 3: .iloc[0] era el PEOR — el juez
        # codificado debe ser exactamente el declarado)
        tope = normales.nlargest(2).iloc[-1] if len(normales) >= 2 else normales.max()
        for m in ANOMALOS & set(rs.index):
            if rs[m] > tope:
                print(f"  (mes anómalo {m}: ratio {rs[m]:.3f} winsorizado a {tope:.3f})",
                      flush=True)
                rs[m] = tope
    gm = float(np.exp(np.log(rs).mean()))
    med = float(rs.median())
    gana_n = int((rs < 1).sum())
    entra = gm < 1 and med < 1
    print(f"\nVEREDICTO ({nombre}): media geométrica de ratios {gm:.3f} | mediana "
          f"{med:.3f} | meses ganados {gana_n}/{len(rs)} — "
          f"{'ENTRA' if entra else 'NO ENTRA'} (juez: geométrica y mediana < 1)",
          flush=True)
    return entra


def _predice_ensamble(vm, hasta_mes, horizonte=HORIZONTE):
    """MÉTODO ADOPTADO (2026-07-31, juez: media geométrica de ratios + 6/6
    meses ganados al ingenuo, WAPE 0.394 vs 0.432): ENSAMBLE 50/50 del GBM
    residual mensual (lags/nivel/intermitencia/mes/tendencia) y el ingenuo
    (mes anterior). La combinación simple es la práctica con mejor evidencia
    (M4: 12 de los 17 mejores fueron combinaciones; ver
    docs/EVALUACION_FORECAST_MENSUAL.md)."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    todas = _features_ml(vm, hasta_mes)
    todas["ratio"] = (todas.y / todas.base.clip(lower=0.5)).clip(0, 5)
    tr = todas[todas.y.notna()]
    Xc = ["l1", "l2", "l3", "lnivel", "ceros6", "mes_cal", "tend"]
    gbm = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.06,
                                        max_depth=6, random_state=7)
    gbm.fit(tr[Xc], tr.ratio)
    # estado al último mes conocido (una fila por SKU, mes_obj = hasta_mes+1…)
    hist = vm.loc[:, vm.columns <= hasta_mes]
    u1 = hist.iloc[:, -1]
    med3 = hist.iloc[:, -3:].median(axis=1)
    base = (0.6 * u1 + 0.4 * med3).clip(lower=0)
    ing = u1.clip(lower=0)
    filas = []
    for h in range(1, horizonte + 1):
        mes_obj = hasta_mes + h
        X = pd.DataFrame({
            "l1": np.log1p(u1), "l2": np.log1p(hist.iloc[:, -2]),
            "l3": np.log1p(hist.iloc[:, -3]),
            "lnivel": np.log1p(hist.iloc[:, -6:].mean(axis=1)),
            "ceros6": (hist.iloc[:, -6:] <= 0).mean(axis=1),
            "mes_cal": mes_obj.month,
            "tend": (u1 / med3.replace(0, np.nan)).fillna(1.0).clip(0, 5)})
        g = pd.Series(gbm.predict(X[Xc]).clip(0, 5), index=vm.index) * base
        pred = (0.5 * g + 0.5 * ing).clip(lower=0)
        filas.append(pd.DataFrame({"codigo": pred.index, "mes": str(mes_obj),
                                   "pred": pred.values.round(2)}))
    return pd.concat(filas, ignore_index=True)


def generar():
    vm, ss = _mensual()
    hoy_m = pd.Timestamp.today().to_period("M")
    ult_cerrado = hoy_m - 1
    os.makedirs(DIR_P, exist_ok=True)
    ruta = os.path.join(DIR_P, f"pred_{hoy_m.strftime('%Y%m')}.parquet")
    if os.path.exists(ruta):
        print(f"generar: ya existe {ruta} (archivo inmutable)", flush=True)
        return
    if not _panel_cubre(ult_cerrado):
        print(f"generar: el panel NO cubre {ult_cerrado} completo — pospuesto "
              f"(corre extract + panel; el cron reintenta mañana)", flush=True)
        return
    pr = _predice_ensamble(vm, ult_cerrado)
    pr["generado_en"] = str(hoy_m)
    # se archiva también el u0 del motor (×4.345) para que el examen lo
    # califique EX-ANTE (auditoría 2026-07-31: usar la corrida vigente le
    # daba lookahead — la corrida actual ya vio el mes examinado)
    ruta_u0 = os.path.join(DATA, "forecast_u0.parquet")
    if os.path.exists(ruta_u0):
        u0 = pd.read_parquet(ruta_u0).set_index("codigo").u0 * 4.345
        pr["u0_mensual"] = pr.codigo.map(u0).round(2)
    pr.to_parquet(ruta, index=False)
    print(f"forecast propio (ensamble GBM+ingenuo) archivado: {ruta} "
          f"({pr.codigo.nunique():,} SKUs × {HORIZONTE} meses desde {ult_cerrado + 1})",
          flush=True)


def examen():
    """Califica el mes recién CERRADO contra lo que cada fuente archivó."""
    vm, _ = _mensual()
    hoy_m = pd.Timestamp.today().to_period("M")
    mes_ex = hoy_m - 1
    ruta_out = os.path.join(BASE, "out", "examen_forecasts.csv")
    prev = pd.read_csv(ruta_out) if os.path.exists(ruta_out) else pd.DataFrame(
        columns=["mes", "fuente", "wape", "n_skus", "generado"])
    if str(mes_ex) in set(prev.mes):
        print(f"examen: {mes_ex} ya calificado", flush=True)
        return
    if mes_ex not in vm.columns:
        print(f"examen: aún no hay ventas completas de {mes_ex}", flush=True)
        return
    if not _panel_cubre(mes_ex):
        print(f"examen: el panel NO cubre {mes_ex} completo — pospuesto "
              f"(calificar con mes parcial dejaría una mancha permanente en el "
              f"marcador; corre extract + panel)", flush=True)
        return
    real = vm[mes_ex]
    real = real[real > 0]
    filas = []

    def _califica(nombre, pred, generado):
        comunes = real.index.intersection(pred.index)
        if len(comunes) < 100:
            return
        w = float((pred.reindex(comunes) - real[comunes]).abs().sum() / real[comunes].sum())
        filas.append({"mes": str(mes_ex), "fuente": nombre, "wape": round(w, 4),
                      "n_skus": len(comunes), "generado": generado})

    # ingenuo (piso) y ESTACIONAL ingenuo (2º piso, recomendación de la
    # investigación 2026-07-31: siempre dos baselines)
    if (mes_ex - 1) in vm.columns:
        _califica("ingenuo (mes anterior)", vm[mes_ex - 1], str(mes_ex - 1))
    if (mes_ex - 12) in vm.columns:
        _califica("ingenuo estacional (año pasado)", vm[mes_ex - 12], str(mes_ex - 12))
    # EQUIDAD DE HORIZONTE (2026-07-31): se califica el archivo MÁS RECIENTE
    # generado ANTES de conocer el mes (≈1 mes de anticipación, mismo horizonte
    # que el ingenuo) — el más viejo castigaría a los modelos con h=5 vs h=1.
    def _archivo_reciente(patron, mes_col, val_col, nombre):
        for r in sorted(glob.glob(patron), reverse=True):
            gen = os.path.basename(r)[5:11]
            if gen > mes_ex.strftime("%Y%m"):
                continue
            df = pd.read_parquet(r)
            if "tipo" in df.columns:
                df = df[df.tipo == "prediction"]
            dfm = df[df[mes_col] == str(mes_ex)]
            if len(dfm):
                _califica(nombre, dfm.groupby("codigo")[val_col].first(), gen)
                return
    _archivo_reciente(os.path.join(DIR_P, "pred_*.parquet"), "mes", "pred",
                      "propio (GBM+ingenuo 50/50)")
    _archivo_reciente(os.path.join(DATA, "aws_forecast", "archivo", "pred_*.parquet"),
                      "mes", "demanda", "AWS")
    # motor u0 mensualizado, desde el ARCHIVO ex-ante (nunca la corrida
    # vigente: ya vio el mes examinado — lookahead; auditoría 2026-07-31)
    for r in sorted(glob.glob(os.path.join(DIR_P, "pred_*.parquet")), reverse=True):
        gen = os.path.basename(r)[5:11]
        if gen > mes_ex.strftime("%Y%m"):
            continue
        df = pd.read_parquet(r)
        if "u0_mensual" in df.columns and str(mes_ex) in set(df.mes):
            _califica("motor u0 (recurrente ×4.345, ex-ante)",
                      df[df.mes == str(mes_ex)].groupby("codigo").u0_mensual.first(),
                      gen)
        break
    out = pd.concat([prev, pd.DataFrame(filas)], ignore_index=True)
    out.to_csv(ruta_out, index=False)
    print(f"== EXAMEN {mes_ex} (WAPE vs venta real; menor = mejor) ==", flush=True)
    for f in sorted(filas, key=lambda x: x["wape"]):
        print(f"  {f['fuente']:<28} {f['wape']:.3f}  ({f['n_skus']:,} SKUs, "
              f"generado {f['generado']})", flush=True)
    print(f"marcador acumulado → {ruta_out}", flush=True)


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    {"backtest": backtest, "backtest-ml": backtest_ml,
     "generar": generar, "examen": examen}[modo]()
