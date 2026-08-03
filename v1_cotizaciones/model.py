# -*- coding: utf-8 -*-
"""Modelo win-rate (bid-response) sobre renglones de cotización.

P(cotización ganada | precio relativo a lista, segmento) con restricción
monotónica decreciente en el precio. Split temporal: entrena hasta mayo-2026,
evalúa jun/jul-2026 (out-of-time).

Correcciones incorporadas:
  1. PESO por cotización (sample_weight): no todas las cotizaciones valen 1;
     el peso refleja el "interés real" estimado (por canal `via` y por
     renglones a lista completa sin negociación).
  2. DEDUP semanal: una sola cotización por cliente-SKU-semana (si alguna
     versión ganó se conserva la ganadora; si no, la última). Evita contar
     una misma negociación como varios rechazos independientes.
  3. LISTA APLICABLE: rel_precio se calcula contra la lista que corresponde
     al renglón (precio 1 o precio 3 según `tipo_precio`), no siempre contra
     la lista alta.

Uso: .venv/bin/python model.py
Salidas: out/modelo_eval.txt, out/dataset_modelo.parquet, out/modelo.joblib
"""
import sys

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, brier_score_loss

FECHA_CORTE_TEST = "2026-06-01"
REL_MIN, REL_MAX = 0.30, 1.05

# --- Corrección 1: peso por cotización -------------------------------------
# Proporción de "interés real" por canal `via` (1.0 = trato atendido normal).
# AJUSTAR las claves a los valores reales del campo `via`; lo no listado pesa 1.0.
# El script imprime la tabla de conversión por canal para ayudar a calibrarlos
# (sugerencia: peso ≈ conversión del canal / conversión del canal atendido).
PESO_VIA = {
    # "WEB": 0.3,
    # "PORTAL": 0.3,
}
# Renglones a lista (casi) completa: mucha cotización automática sin negociación.
PESO_LISTA_COMPLETA = 0.5
# ---------------------------------------------------------------------------
TOP_LINEAS = 40
TOP_PROV = 30
TOP_CLASIF = 12
TOP_SUC = 15


def construir_dataset():
    con = duckdb.connect()
    df = con.execute(f"""
        WITH costo_ult AS (
            SELECT id_art, id_ast,
                   arg_max(cos_prom_dlls, fecha) AS cos_prom_dlls
            FROM 'data/costos.parquet'
            WHERE cos_prom_dlls > 0
            GROUP BY id_art, id_ast
        )
        SELECT l.id_cot, l.fecha_alta, l.won, l.id_cliente, l.id_sucursal,
               l.id_art, l.id_asterisco, l.cantidad, l.precio, l.precio_lista,
               l.tipo_precio, l.total_art, l.via,
               a.linea, a.marca, a.id_proveedor, a.clasificacion AS clasif_art,
               a.precio_1, a.factor_precio_1, a.factor_precio_3, a.moneda AS moneda_art,
               c.cos_prom_dlls,
               cl.clasif_cliente
        FROM 'data/lineas.parquet' l
        LEFT JOIN 'data/articulos.parquet' a ON a.id_art = l.id_art
        LEFT JOIN costo_ult c ON c.id_art = l.id_art AND c.id_ast = l.id_asterisco
        LEFT JOIN 'data/clientes.parquet' cl ON cl.id_clientes = l.id_cliente
        WHERE l.precio > 0 AND l.precio_lista > 0
          AND l.cantidad > 0
          AND (l.id_kit IS NULL OR l.id_kit = 0)   -- fuera componentes de kit
          -- filtro amplio de sanidad; el filtro fino se aplica en pandas
          -- contra la lista aplicable (precio 1 o 3 según tipo_precio)
          AND l.precio / l.precio_lista BETWEEN 0.05 AND 3.0
    """).df()
    con.close()

    df["won"] = df["won"].astype(int)
    for col in ("factor_precio_1", "factor_precio_3", "cos_prom_dlls"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # --- Corrección 3: lista aplicable según tipo_precio ---------------------
    # Supuesto: las listas se derivan del costo (precio_1 = costo × factor_1,
    # precio_3 = costo × factor_3), por lo que lista_3 = lista_1 × f3/f1.
    # VERIFICAR: en renglones con tipo_precio==3, la mediana de
    # precio/lista_aplicable debe quedar cerca de 1.0 (se imprime abajo).
    f_ok = (df["factor_precio_1"] > 0) & (df["factor_precio_3"] > 0)
    lista_3 = pd.Series(
        np.where(f_ok, df["precio_lista"].astype(float)
                 * df["factor_precio_3"] / df["factor_precio_1"], np.nan),
        index=df.index)
    df["lista_aplicable"] = np.where(
        (df["tipo_precio"] == 3) & (lista_3 > 0),
        lista_3, df["precio_lista"].astype(float))

    df["rel_precio"] = (df["precio"] / df["lista_aplicable"]).astype(float)
    df = df[df["rel_precio"].between(REL_MIN, REL_MAX)].copy()

    chk = df.loc[df["tipo_precio"] == 3, "rel_precio"]
    if len(chk):
        print(f"sanity lista_3: mediana rel_precio en tipo_precio==3 = "
              f"{chk.median():.3f} (esperado ~1.0)", flush=True)

    df["log_cantidad"] = np.log1p(df["cantidad"].astype(float))
    df["log_monto"] = np.log1p((df["precio"] * df["cantidad"]).astype(float))
    df["log_total_art"] = np.log1p(df["total_art"].fillna(1).astype(float))
    df["mes"] = pd.to_datetime(df["fecha_alta"]).dt.month
    df["es_precio_3"] = (df["tipo_precio"] == 3).astype(int)

    # ratio costo/lista APLICABLE: costo promedio real; fallback al factor
    # de catálogo que corresponde a la lista usada (1/f1 o 1/f3)
    c_prom = df["cos_prom_dlls"] / df["lista_aplicable"]
    c_prom = c_prom.where((c_prom > 0.02) & (c_prom < 0.98))
    factor_apl = np.where(df["tipo_precio"] == 3,
                          df["factor_precio_3"], df["factor_precio_1"])
    c_cat = pd.Series(np.where(factor_apl > 1, 1.0 / factor_apl, np.nan),
                      index=df.index)
    df["costo_sobre_lista"] = c_prom.fillna(c_cat)

    # --- Corrección 2: dedup — una cotización por cliente-SKU-semana ---------
    # Si alguna versión ganó, se conserva la ganadora (ese precio cerró);
    # si ninguna ganó, la más reciente. Evita contar contraofertas de una
    # misma negociación como rechazos independientes.
    df["semana"] = pd.to_datetime(df["fecha_alta"]).dt.to_period("W").astype(str)
    grupo = ["id_cliente", "id_art", "id_asterisco", "semana"]
    antes = len(df)
    df = (df.sort_values(["won", "fecha_alta"])   # won=1 y más reciente al final
            .drop_duplicates(subset=grupo, keep="last")
            .reset_index(drop=True))
    print(f"dedup semanal cliente-SKU: {antes} -> {len(df)} renglones "
          f"({antes - len(df)} duplicados colapsados)", flush=True)

    # --- Corrección 1: peso por cotización -----------------------------------
    df["peso"] = df["via"].map(PESO_VIA).fillna(1.0).astype(float)
    df.loc[df["rel_precio"] >= 0.995, "peso"] *= PESO_LISTA_COMPLETA
    # tabla de apoyo para calibrar PESO_VIA con datos reales
    tab = (df.groupby("via", dropna=False)
             .agg(n=("won", "size"), conv=("won", "mean"),
                  peso=("peso", "mean")).sort_values("n", ascending=False))
    print("conversión por canal (para calibrar PESO_VIA):", flush=True)
    print(tab.to_string(), flush=True)
    return df


CATS = {
    "cat_linea": ("linea", "_na", TOP_LINEAS),
    "cat_prov": ("id_proveedor", -1, TOP_PROV),
    "cat_cli": ("clasif_cliente", "_na", TOP_CLASIF),
    "cat_suc": ("id_sucursal", -1, TOP_SUC),
}


def construir_mapas(df):
    """Mapa valor→código por categórica, fijado con el set de entrenamiento."""
    mapas = {}
    for col, (origen, relleno, top_n) in CATS.items():
        s = df[origen].fillna(relleno)
        top = s.value_counts().head(top_n).index
        mapas[col] = {v: i + 1 for i, v in enumerate(top)}
    return mapas


def preparar_matriz(df, mapas):
    numericas = ["rel_precio", "rel_vs_tipico", "log_cantidad", "log_monto",
                 "log_total_art", "mes", "es_precio_3"]
    X = df[numericas].astype(np.float32).copy()
    for col, (origen, relleno, _) in CATS.items():
        X[col] = df[origen].fillna(relleno).map(mapas[col]).fillna(0).astype(np.float32)
    return X


def evaluar(nombre, modelo, Xtr, ytr, Xte, yte, wtr, wte, out):
    """Métricas ponderadas por peso: se valida contra la misma población
    ponderada con la que se entrena (si no, la ponderación no se mediría)."""
    ptr, pte = modelo.predict_proba(Xtr)[:, 1], modelo.predict_proba(Xte)[:, 1]
    wtr = np.asarray(wtr, dtype=float)
    wte = np.asarray(wte, dtype=float)
    lineas = [
        f"== {nombre} ==",
        f"AUC train: {roc_auc_score(ytr, ptr, sample_weight=wtr):.4f}   "
        f"AUC test (out-of-time): {roc_auc_score(yte, pte, sample_weight=wte):.4f}",
        f"Brier test: {brier_score_loss(yte, pte, sample_weight=wte):.4f}   "
        f"base rate test (pond): {np.average(yte, weights=wte):.4f}   "
        f"pred media (pond): {np.average(pte, weights=wte):.4f}",
    ]
    # calibración ponderada por decil de probabilidad
    q = pd.qcut(pte, 10, duplicates="drop")
    tmp = pd.DataFrame({"pred": pte, "real": np.asarray(yte), "w": wte})
    cal = tmp.groupby(q, observed=True).apply(
        lambda g: pd.Series({"pred": np.average(g["pred"], weights=g["w"]),
                             "real": np.average(g["real"], weights=g["w"])}))
    lineas.append("calibración ponderada (decil pred vs real):")
    for _, r in cal.iterrows():
        lineas.append(f"   pred {r['pred']:.3f}  real {r['real']:.3f}")
    print("\n".join(lineas), flush=True)
    out.extend(lineas + [""])
    return pte


def curva_pwin(modelo, X, base_idx, grid):
    """P(win) promedio al barrer el precio en grid sobre una muestra de renglones.

    Mueve rel_precio y rel_vs_tipico juntos (el nivel típico del SKU queda fijo).
    """
    base = X.iloc[base_idx].copy()
    med = base["rel_precio"] - base["rel_vs_tipico"]
    curvas = []
    for r in grid:
        b = base.copy()
        b["rel_precio"] = r
        b["rel_vs_tipico"] = r - med
        curvas.append(modelo.predict_proba(b)[:, 1].mean())
    return curvas


def main():
    df = construir_dataset()
    print(f"dataset: {len(df)} renglones, win rate {df.won.mean():.3f}", flush=True)

    # precio vs el nivel típico del propio SKU (mediana en TRAIN para no filtrar futuro)
    df = df.reset_index(drop=True)
    es_train = pd.to_datetime(df["fecha_alta"]) < FECHA_CORTE_TEST
    med_sku = (df[es_train].groupby(["id_art", "id_asterisco"])["rel_precio"]
               .median().rename("rel_tipico_sku").reset_index())
    df = df.merge(med_sku, on=["id_art", "id_asterisco"], how="left")
    df["rel_tipico_sku"] = df["rel_tipico_sku"].fillna(df["rel_precio"])
    df["rel_vs_tipico"] = (df["rel_precio"] - df["rel_tipico_sku"]).astype(np.float32)

    es_test = pd.to_datetime(df["fecha_alta"]) >= FECHA_CORTE_TEST
    df.to_parquet("out/dataset_modelo.parquet", index=False)
    mapas = construir_mapas(df[~es_test])
    X = preparar_matriz(df, mapas)
    y = df["won"]
    w = df["peso"].astype(float)
    Xtr, ytr, Xte, yte = X[~es_test], y[~es_test], X[es_test], y[es_test]
    wtr, wte = w[~es_test], w[es_test]
    print(f"train: {len(Xtr)} ({ytr.mean():.3f})  test: {len(Xte)} ({yte.mean():.3f})"
          f"  peso medio train: {wtr.mean():.3f}", flush=True)

    out = [f"dataset {len(df)} | train {len(Xtr)} | test {len(Xte)}", ""]

    mono = np.zeros(X.shape[1], dtype=int)
    mono[X.columns.get_loc("rel_precio")] = -1
    mono[X.columns.get_loc("rel_vs_tipico")] = -1
    gbm = HistGradientBoostingClassifier(
        monotonic_cst=mono, max_iter=300, learning_rate=0.08,
        max_leaf_nodes=63, min_samples_leaf=200, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.1, random_state=42)
    gbm.fit(Xtr, ytr, sample_weight=wtr)
    evaluar("GBM monotónico (ponderado)", gbm, Xtr, ytr, Xte, yte, wtr, wte, out)

    # baseline univariada: logística solo con rel_precio (sanity de pendiente)
    logit = LogisticRegression(max_iter=1000)
    logit.fit(Xtr[["rel_precio"]], ytr, sample_weight=wtr)
    evaluar("Logística rel_precio (baseline)", logit,
            Xtr[["rel_precio"]], ytr, Xte[["rel_precio"]], yte, wtr, wte, out)

    # curva P(win|precio) — sanity de pendiente
    rng = np.random.RandomState(0)
    idx = rng.choice(len(Xte), size=min(4000, len(Xte)), replace=False)
    grid = np.round(np.arange(0.60, 1.041, 0.04), 2)
    curva = curva_pwin(gbm, Xte.reset_index(drop=True), idx, grid)
    out.append("curva P(win) vs rel_precio (muestra test):")
    for r, p in zip(grid, curva):
        out.append(f"   r={r:.2f}  P(win)={p:.3f}")
    print("\n".join(out[-len(grid) - 1:]), flush=True)

    joblib.dump({"modelo": gbm, "columnas": list(X.columns), "mapas": mapas}, "out/modelo.joblib")
    with open("out/modelo_eval.txt", "w") as f:
        f.write("\n".join(out))
    print("OK: out/modelo.joblib, out/modelo_eval.txt", flush=True)


if __name__ == "__main__":
    sys.exit(main())
