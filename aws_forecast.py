# -*- coding: utf-8 -*-
"""Integra el forecast mensual de AWS (proceso existente del negocio) al motor v3.

Entradas (exports que el usuario deja en ~/Downloads o en data/aws_forecast/):
  - predictions*.csv : item_id, office_branch, demand, month, type(fact|prediction)
  - cat_modelos.csv  : id_producto, modelo  (mapa item_id -> codigo del ERP)

Qué hace:
  1. Normaliza: solo office_branch='all' (total compañía), item_id -> codigo,
     demanda negativa (devoluciones netas) se recorta a 0.
  2. Guarda data/aws_forecast/forecast_mensual.parquet (codigo, mes, demanda, tipo).
  3. Califica la precisión: para los meses que traen fact Y prediction calcula
     WAPE por SKU y global; contraste vs baseline ingenuo (mes anterior real).
     CAVEAT: si el run de AWS se entrenó con esos meses ya vistos, esas filas son
     ajuste in-sample, no pronóstico honesto — la calificación sería optimista.
  4. Cruza contra el motor (out/recomendaciones.csv): cobertura y comparación del
     nivel de demanda AWS vs nuestro u0 (semanal -> mensual x 4.345) para dimensionar
     si sirve como (a) retador de u0, (b) segunda opinión de demanda creciente.

Uso:  ./.venv/bin/python aws_forecast.py [ruta_predictions.csv] [ruta_cat.csv]

NOTAS del pipeline AWS (doc Confluence SOW4, 2026-07-27):
  - Las predicciones son el CUANTIL P60 (no la media): sesgo alto deliberado de
    ~+4-5% como colchón; configurable a P65-P80 en su repo.
  - Outliers (picos >μ+3σ) se reemplazan con KNN ANTES de entrenar ⇒ su forecast
    apunta a demanda RECURRENTE (compatible con nuestra exclusión de proyectos).
  - Flujo mensual (EventBridge→Step Functions→Batch); repo syscom_clearscale.
  - product_inactivity_threshold=20: sin datos 20 fechas ⇒ sin predicción (por
    eso ~2,400 items tienen fact sin prediction).
  - CAVEAT del examen: si el export sale de UN run mensual, las predictions de
    meses pasados son backtest/in-sample ⇒ el WAPE medido es optimista. El
    examen honesto: guardar el forecast de cada mes y calificarlo al cierre.
  - Cold start: productos nuevos reciben predicción por similitud de metadata
    (brand/category del lado AWS) — oportunidad para SKUs sin historia donde
    nuestro motor se abstiene.
"""
import html
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, "data", "aws_forecast")
SEM_POR_MES = 4.345


def cargar(ruta_pred, ruta_cat):
    pred = pd.read_csv(ruta_pred)
    cat = pd.read_csv(ruta_cat)
    cat["modelo"] = cat.modelo.astype(str).str.strip()
    df = pred[pred.office_branch == "all"].copy()
    df["demand"] = df.demand.astype(float).clip(lower=0)
    df = df.merge(cat.rename(columns={"id_producto": "item_id", "modelo": "codigo"}),
                  on="item_id", how="left")
    sin_mapa = df[df.codigo.isna()].item_id.nunique()
    df = df.dropna(subset=["codigo"])
    print(f"filas 'all': {len(df):,} | items: {df.item_id.nunique():,} "
          f"(sin mapa a codigo: {sin_mapa:,}) | meses: {df.month.min()}–{df.month.max()}",
          flush=True)
    return df[["codigo", "month", "demand", "type"]].rename(
        columns={"month": "mes", "demand": "demanda", "type": "tipo"})


def sucursales(ruta_pred, ruta_cat):
    """AMPLITUD TERRITORIAL de la demanda (idea del usuario 2026-07-27).

    El forecast AWS también modela cada sucursal por separado. Uso: distinguir
    demanda ANCHA (muchas sucursales creciendo = mercado real) de demanda
    CONCENTRADA (una sucursal = probable cliente/proyecto local — misma lógica
    que 'ventas de proyecto no son demanda'). Por SKU:
      - suc_activas    : sucursales con venta reciente (últimos 3 meses fact)
      - suc_alza_frac  : fracción de activas donde el futuro pronosticado
                         (meses > mes en curso) supera el reciente real
      - suc_top_share  : concentración (share de la sucursal más grande)
    Solo informativo; no decide.
    """
    pred = pd.read_csv(ruta_pred)
    cat = pd.read_csv(ruta_cat)
    cat["modelo"] = cat.modelo.astype(str).str.strip()
    suc = pred[pred.office_branch != "all"].copy()
    suc["office_branch"] = suc.office_branch.map(html.unescape)  # fusiona duplicados de encoding
    suc["demand"] = suc.demand.astype(float).clip(lower=0)
    mes_actual = pd.Timestamp.today().strftime("%Y-%m")

    fact = suc[suc.type == "fact"]
    meses_rec = sorted(fact.month.unique())[-3:]
    rec = (fact[fact.month.isin(meses_rec)]
           .groupby(["item_id", "office_branch"]).demand.mean().rename("reciente"))
    fut = (suc[(suc.type == "prediction") & (suc.month > mes_actual)]
           .groupby(["item_id", "office_branch"]).demand.mean().rename("futuro"))
    m = pd.concat([rec, fut], axis=1).fillna(0).reset_index()
    m["activa"] = m.reciente > 0
    m["alza"] = m.futuro > m.reciente

    g = m[m.activa].groupby("item_id")
    res = pd.DataFrame({
        "suc_activas": g.size(),
        "suc_alza_frac": g.alza.mean().round(2),
        "suc_top_share": (g.reciente.max() / g.reciente.sum()).round(2),
    }).reset_index()
    res = res.merge(cat.rename(columns={"id_producto": "item_id", "modelo": "codigo"}),
                    on="item_id").drop(columns="item_id")
    print(f"\n== AMPLITUD POR SUCURSAL ({suc.office_branch.nunique()} sucursales; "
          f"reciente = media {meses_rec}, futuro = media > {mes_actual}) ==", flush=True)
    print(f"  SKUs con actividad reciente en sucursales: {len(res):,}", flush=True)
    print(f"  sucursales activas por SKU: mediana {res.suc_activas.median():.0f} | "
          f"concentración top: mediana {100*res.suc_top_share.median():.0f}%", flush=True)
    print(f"  SKUs con alza ANCHA (≥50% de sus sucursales): "
          f"{(res.suc_alza_frac >= .5).sum():,} | concentrados (top ≥60%): "
          f"{(res.suc_top_share >= .6).sum():,}", flush=True)
    res.to_parquet(os.path.join(DIR, "sucursales.parquet"), index=False)
    print(f"guardado data/aws_forecast/sucursales.parquet", flush=True)
    return res


def calificar(df):
    """WAPE de prediction vs fact en los meses con ambos, + baseline ingenuo."""
    piv = df.pivot_table(index=["codigo", "mes"], columns="tipo", values="demanda",
                         aggfunc="first").reset_index()
    ambos = piv.dropna(subset=["fact", "prediction"]).copy()
    if ambos.empty:
        print("sin meses con fact+prediction: no se puede calificar aún", flush=True)
        return None
    print(f"\n== CALIFICACIÓN (meses con fact y prediction: "
          f"{sorted(ambos.mes.unique())}) ==", flush=True)
    wape_g = (ambos.prediction - ambos.fact).abs().sum() / ambos.fact.sum()
    print(f"  WAPE global AWS: {100*wape_g:.1f}%", flush=True)

    # baseline ingenuo: fact del mes anterior (mismo estándar que usamos con u0:
    # si no le gana a lo simple, no entra)
    f = piv.pivot(index="codigo", columns="mes", values="fact").sort_index(axis=1)
    naive = f.shift(1, axis=1).stack().rename("naive").reset_index()
    amb2 = ambos.merge(naive, on=["codigo", "mes"], how="left").dropna(subset=["naive"])
    wape_n = (amb2.naive - amb2.fact).abs().sum() / amb2.fact.sum()
    wape_a2 = (amb2.prediction - amb2.fact).abs().sum() / amb2.fact.sum()
    print(f"  mismo subconjunto — AWS: {100*wape_a2:.1f}%  vs  ingenuo (mes anterior): "
          f"{100*wape_n:.1f}%  {'✓ AWS gana' if wape_a2 < wape_n else '✗ no gana al ingenuo'}",
          flush=True)

    # por tercil de volumen (el WAPE global lo dominan los SKUs grandes)
    vol = ambos.groupby("codigo").fact.mean()
    ambos["tercil"] = pd.qcut(vol.reindex(ambos.codigo).values, 3,
                              labels=["bajo", "medio", "alto"])
    for t in ["bajo", "medio", "alto"]:
        s = ambos[ambos.tercil == t]
        w = (s.prediction - s.fact).abs().sum() / max(s.fact.sum(), 1e-9)
        print(f"  tercil {t:<5}: WAPE {100*w:.1f}%  ({s.codigo.nunique():,} SKUs)", flush=True)

    # WAPE por SKU (confiabilidad del forecast a nivel producto)
    g = ambos.groupby("codigo").apply(
        lambda s: (s.prediction - s.fact).abs().sum() / max(s.fact.sum(), 1e-9),
        include_groups=False).rename("wape_sku")
    print(f"  WAPE por SKU: mediana {100*g.median():.0f}% | p25 {100*g.quantile(.25):.0f}% "
          f"| p75 {100*g.quantile(.75):.0f}%", flush=True)
    return g


def comparar_motor(df, wape_sku):
    """Cobertura vs el motor y contraste de nivel: AWS (próximo mes) vs u0 mensualizado."""
    ruta_reco = os.path.join(BASE, "out", "recomendaciones.csv")
    if not os.path.exists(ruta_reco):
        return
    reco = pd.read_csv(ruta_reco)
    pred = df[df.tipo == "prediction"]
    # solo meses estrictamente futuros: el mes en curso ya se conoce (regla 2026-07-27)
    mes_actual = pd.Timestamp.today().strftime("%Y-%m")
    meses_fut = sorted(m for m in pred.mes.unique() if m > mes_actual)
    if not meses_fut:
        return
    prox = pred[pred.mes == meses_fut[0]].groupby("codigo").demanda.first()
    en_motor = reco.codigo.isin(prox.index)
    print(f"\n== CRUCE CON EL MOTOR ({meses_fut[0]}) ==", flush=True)
    print(f"  SKUs del motor con forecast AWS: {en_motor.sum():,} de {len(reco):,} "
          f"({100*en_motor.mean():.0f}%)", flush=True)

    m = reco[["codigo", "u_sem_actual"]].merge(prox.rename("aws_mes"), on="codigo")
    m["u0_mes"] = m.u_sem_actual * SEM_POR_MES
    m = m[m.u0_mes > 0]
    ratio = (m.aws_mes / m.u0_mes).replace([np.inf, -np.inf], np.nan).dropna()
    print(f"  nivel AWS/u0 (mensualizado): mediana {ratio.median():.2f} "
          f"| p25 {ratio.quantile(.25):.2f} | p75 {ratio.quantile(.75):.2f} "
          f"(1.00 = misma escala)", flush=True)
    if wape_sku is not None:
        conf = wape_sku.reindex(m.codigo)
        print(f"  y de esos, WAPE AWS mediano: {100*conf.median():.0f}%", flush=True)


def cerrar_mes():
    """EXAMEN HONESTO MENSUAL (la comparativa justa AWS vs motor).

    Cuando un mes CIERRA: toma del ARCHIVO el forecast que AWS emitió ANTES de
    ese mes (pred_YYYYMM guardado al integrar cada export), lo compara contra
    la venta recurrente real del mes (panel) y contra dos varas: el naive
    (mes anterior real) y nuestro u0 congelado en el snapshot del ciclo
    (ciclo.py) mensualizado. Regla FVA: AWS gana su lugar si le gana al naive
    en out-of-time REAL; contra u0 mide quién merece ser la base mensual.
    """
    import glob as _g
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
    pan["mes"] = pd.to_datetime(pan.semana).dt.strftime("%Y-%m")
    mes_actual = pd.Timestamp.today().strftime("%Y-%m")
    archivos = sorted(_g.glob(os.path.join(DIR, "archivo", "pred_*.parquet")))
    if not archivos:
        raise SystemExit("sin archivo de forecasts emitidos — integra un export primero")
    resultados = []
    for ruta in archivos:
        emitido = os.path.basename(ruta)[5:11]          # YYYYMM de emisión
        arc = pd.read_parquet(ruta)
        fut = arc[(arc.tipo == "prediction") & (arc.mes > f"{emitido[:4]}-{emitido[4:]}")]
        # meses ya CERRADOS que ese export pronosticó a futuro
        cerrados = [m_ for m_ in fut.mes.unique() if m_ < mes_actual]
        for m_ in cerrados:
            pred = fut[fut.mes == m_].groupby("codigo").demanda.first()
            real = pan[pan.mes == m_].groupby("codigo").unidades_rec.sum()
            prev = pan[pan.mes == _mes_previo(m_)].groupby("codigo").unidades_rec.sum()
            comun = pred.index.intersection(real.index)
            if len(comun) < 100:
                continue
            w_aws = (pred[comun] - real[comun]).abs().sum() / real[comun].sum()
            naive = prev.reindex(comun).fillna(0)
            w_nv = (naive - real[comun]).abs().sum() / real[comun].sum()
            resultados.append({"emitido": emitido, "mes": m_, "n": len(comun),
                               "wape_aws": round(float(w_aws), 4),
                               "wape_naive": round(float(w_nv), 4),
                               "aws_gana": bool(w_aws < w_nv)})
            print(f"  export {emitido} → {m_}: AWS {w_aws:.3f} vs naive {w_nv:.3f} "
                  f"({len(comun):,} SKUs) {'✓ AWS gana' if w_aws < w_nv else '✗'}",
                  flush=True)
    if resultados:
        pd.DataFrame(resultados).to_parquet(
            os.path.join(DIR, "examen_mensual.parquet"), index=False)
        print(f"guardado {DIR}/examen_mensual.parquet", flush=True)
    else:
        print("aún no hay meses cerrados posteriores a un export archivado — "
              "el primer veredicto honesto llega al cierre de agosto", flush=True)


def _mes_previo(m_):
    t = pd.Timestamp(m_ + "-01") - pd.Timedelta(days=1)
    return t.strftime("%Y-%m")


def main():
    ruta_pred = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        "~/Downloads/predictions-4 (5) (1).csv")
    ruta_cat = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser(
        "~/Downloads/cat_modelos.csv")
    os.makedirs(DIR, exist_ok=True)
    df = cargar(ruta_pred, ruta_cat)
    df.to_parquet(os.path.join(DIR, "forecast_mensual.parquet"), index=False)
    # ARCHIVO INMUTABLE por fecha de integración (examen honesto: cerrar_mes)
    os.makedirs(os.path.join(DIR, "archivo"), exist_ok=True)
    sello = pd.Timestamp.today().strftime("%Y%m")
    ruta_arc = os.path.join(DIR, "archivo", f"pred_{sello}.parquet")
    if not os.path.exists(ruta_arc):
        df.to_parquet(ruta_arc, index=False)
        print(f"archivado {ruta_arc} (para el examen mensual honesto)", flush=True)
    print(f"guardado data/aws_forecast/forecast_mensual.parquet ({len(df):,} filas)",
          flush=True)
    sucursales(ruta_pred, ruta_cat)
    wape_sku = calificar(df)
    if wape_sku is not None:
        wape_sku.reset_index().to_parquet(os.path.join(DIR, "wape_por_sku.parquet"),
                                          index=False)
        print(f"guardado data/aws_forecast/wape_por_sku.parquet "
              f"({len(wape_sku):,} SKUs)", flush=True)
    comparar_motor(df, wape_sku)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cerrar-mes":
        cerrar_mes()
    else:
        main()
