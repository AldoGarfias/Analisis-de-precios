# -*- coding: utf-8 -*-
"""ETAPA 2 — MODELO BID-RESPONSE: P(ganar | rel_precio, contexto), GBM
MONOTÓNICO en el precio (como el v1, sobre la BD nueva). Aprobado 2026-08-01.

JUEZ FIJADO ANTES DE CORRER (filosofía campeón-retador):
  - Split TEMPORAL honesto: entrena cotizaciones < 2026-01-01, evalúa 2026+.
  - Retador (GBM con contexto) ENTRA si su AUC out-of-time supera al mejor
    baseline por ≥ +0.01 en AMBAS definiciones de ganada. Baselines:
    (a) tasa constante, (b) logística solo-rel_precio.
  - Se reporta Brier (calibración) — informativo, no veto.

DOS DEFINICIONES DE GANADA (blindaje del hallazgo):
  - AMPLIA   = venta Activa mismo cliente+SKU ≤28 días (la del dataset).
  - ESTRICTA = amplia Y el neto facturado coincide con el cotizado (±1%) —
    la misma negociación con certeza; excluye al comprador recurrente.

CONTEXTO (features): rel_precio (restricción MONOTÓNICA: a menor precio
relativo, P(ganar) no puede bajar), canal, tipo_precio, mes, tamaño del
folio, log cantidad, log lista, rotación del SKU (panel), y el CONTROL del
comprador frecuente: nº de semanas con compra del mismo cliente-SKU en las
26 previas (el confusor que infla la ganada amplia).

Salida: data/winrate_modelo_{amplia,estricta}.joblib +
data/winrate_curvas_modelo.parquet (curva P(ganar) vs descuento, contexto
fijo, por canal) + impresión del veredicto. NO cambia reglas del motor.
"""
import os

import duckdb
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
R61 = os.path.join(DATA, "reporte61")
CORTE_OOT = "2026-01-01"


def dataset():
    con = duckdb.connect()
    con.execute("SET threads TO 6")
    q = f"""
    WITH neg AS (
      SELECT * FROM read_parquet('{DATA}/winrate_dataset.parquet')
    ),
    vta AS (
      SELECT codigo_cliente AS cli, codigo, CAST(fecha AS DATE) AS fv,
             TRY_CAST(subtotal AS DOUBLE)/TRY_CAST(cantidad AS DOUBLE) AS neto_vta
      FROM read_parquet('{R61}/ventas_*.parquet')
      WHERE TRY_CAST(precio AS DOUBLE) > 0 AND TRY_CAST(cantidad AS DOUBLE) > 0
    )
    SELECT n.*,
      -- ganada ESTRICTA: factura al precio cotizado (±1%) en la ventana
      COALESCE((SELECT bool_or(ABS(v.neto_vta / (n.subtotal/n.cantidad) - 1) <= 0.01)
        FROM vta v WHERE v.cli = n.codigo_cliente AND v.codigo = n.codigo
         AND v.fv BETWEEN n.fecha AND n.fecha + INTERVAL 28 DAY), FALSE) AS ganada_estricta,
      -- control comprador frecuente: semanas con compra en las 26 previas
      (SELECT COUNT(DISTINCT date_trunc('week', v.fv)) FROM vta v
        WHERE v.cli = n.codigo_cliente AND v.codigo = n.codigo
          AND v.fv >= n.fecha - INTERVAL 182 DAY AND v.fv < n.fecha) AS freq_previa
    FROM neg n
    """
    df = con.execute(q).df()
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"),
                          columns=["codigo", "unidades_rec"])
    rot = pan.groupby("codigo").unidades_rec.mean()
    df["rot_sku"] = df.codigo.map(rot).fillna(0.0)
    df["n_folio"] = (1 / df.w).round().astype(int)
    df["mes"] = pd.to_datetime(df.fecha).dt.month
    df["es_linea"] = (df.canal == "linea").astype(int)
    df.to_parquet(os.path.join(DATA, "winrate_dataset_full.parquet"), index=False)
    print(f"dataset: {len(df):,} | amplia {df.ganada.mean():.1%} | "
          f"estricta {df.ganada_estricta.mean():.1%} | "
          f"freq_previa>0: {(df.freq_previa > 0).mean():.1%}", flush=True)
    return df


FEATS = ["rel_precio", "es_linea", "tipo_precio", "mes", "n_folio",
         "log_cant", "log_lista", "rot_sku", "freq_previa"]


def correr():
    ruta = os.path.join(DATA, "winrate_dataset_full.parquet")
    df = pd.read_parquet(ruta) if os.path.exists(ruta) else dataset()
    df["log_cant"] = np.log1p(df.cantidad)
    df["log_lista"] = np.log1p(df.precio)
    df["fecha"] = pd.to_datetime(df.fecha)
    tr, te = df[df.fecha < CORTE_OOT], df[df.fecha >= CORTE_OOT]
    print(f"train {len(tr):,} (<{CORTE_OOT}) | test OOT {len(te):,}", flush=True)

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, brier_score_loss
    import joblib

    mono = [-1 if f == "rel_precio" else 0 for f in FEATS]
    curvas = []
    for target in ("ganada", "ganada_estricta"):
        yt, ye = tr[target].astype(int), te[target].astype(int)
        # baselines
        p_const = np.full(len(te), yt.mean())
        lg = LogisticRegression().fit(tr[["rel_precio"]], yt)
        p_lg = lg.predict_proba(te[["rel_precio"]])[:, 1]
        auc_lg = roc_auc_score(ye, p_lg)
        # retador GBM monotónico
        gb = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.08, max_depth=6, random_state=7,
            monotonic_cst=mono)
        gb.fit(tr[FEATS], yt)
        p_gb = gb.predict_proba(te[FEATS])[:, 1]
        auc_gb = roc_auc_score(ye, p_gb)
        entra = auc_gb >= auc_lg + 0.01
        print(f"\n[{target}] AUC OOT — GBM {auc_gb:.3f} vs logística "
              f"{auc_lg:.3f} (const Brier {brier_score_loss(ye, p_const):.3f} → "
              f"GBM {brier_score_loss(ye, p_gb):.3f}) ⇒ "
              f"{'ENTRA' if entra else 'NO ENTRA'} (juez: +0.01 AUC)", flush=True)
        joblib.dump(gb, os.path.join(DATA, f"winrate_modelo_{target}.joblib"))
        # curva P(ganar) vs descuento con contexto FIJO (mediana del canal
        # vendedor, comprador NO frecuente) — la evidencia para el vendedor
        base = te[(te.es_linea == 0) & (te.freq_previa == 0)][FEATS].median()
        rels = np.arange(0.55, 1.01, 0.025)
        X = pd.DataFrame([base] * len(rels))
        X["rel_precio"] = rels
        pw = gb.predict_proba(X[FEATS])[:, 1]
        for r_, p_ in zip(rels, pw):
            curvas.append({"target": target, "rel_precio": round(r_, 3),
                           "desc_pct": round(100 * (1 - r_), 1),
                           "p_ganar": round(float(p_), 4)})
    C = pd.DataFrame(curvas)
    C.to_parquet(os.path.join(DATA, "winrate_curvas_modelo.parquet"), index=False)
    print("\n== P(GANAR) vs DESCUENTO — canal vendedor, cliente NO frecuente, "
          "contexto mediano (modelo, OOT) ==", flush=True)
    for tgt in ("ganada", "ganada_estricta"):
        g = C[C.target == tgt]
        print(f"  {tgt}:", flush=True)
        for _, x in g[g.desc_pct.isin([0.0, 10.0, 20.0, 30.0, 40.0, 45.0])].iterrows():
            print(f"    desc {x.desc_pct:>4.0f}% → P(ganar) {x.p_ganar:6.1%}  "
                  f"{'█' * int(60 * x.p_ganar)}", flush=True)
    return C


if __name__ == "__main__":
    correr()
