# -*- coding: utf-8 -*-
"""REGISTRO E INTELIGENCIA DE COMPETENCIA (usuario 2026-08-07: "primero un
registro por cada competidor —una BD por competidor—, después identificar
cambios, ANTES de pasar a la comparativa con nuestros modelos").

Capa 1 — REGISTRO (`consolidar`): una BD por competidor en
  data/competencia/db/<fuente>.parquet — historia modelo×fecha con
  precio_lista, descuento_pct, precio_venta, moneda, existencia, marca.
  Se alimenta de los CSV crudos del feed (extract_competencia.py) por
  merge idempotente (dedup modelo+fecha, gana el más reciente).

Capa 2 — CAMBIOS (`cambios`): por competidor y modelo, comparando fechas
  consecutivas EN SU PROPIA MONEDA (jamás cruzar FX aquí — el tipo de cambio
  metería ruido de cambios falsos):
    PRECIO SUBIÓ/BAJÓ  |Δprecio_venta| ≥ 1%
    ALTA               modelo aparece por primera vez
    STOCKOUT           existencia >0 → 0
    REABASTECIDO       existencia 0 → >0
  Salida: data/competencia/cambios.parquet (historia completa) +
  out/competencia_cambios.csv (últimos 7 días, para revisión rápida).

Capa 3 — MATCHING CON SYSCOM (`match_syscom`): para cada modelo de la
  competencia encuentra el modelo SYSCOM que le corresponde:
    EXACTO          igual tras normalizar (quitar no-alfanuméricos)
    FUZZY_ALTO      score ≥ 0.70 (TF-IDF n-gramas 2-4 + re-rank RapidFuzz)
    FUZZY_MEDIO     score 0.50–0.70 (requiere revisión humana)
    SIN_COINCIDENCIA score < 0.50
  Catálogo: ~/Downloads/cat_modelos.csv (id_producto, modelo). Salida:
  data/competencia/match_syscom.parquet + out/competencia_match_syscom.csv
  (solo candidatos ALTO/MEDIO, para revisión rápida).

Cron: corre a diario en la cadena de 8:30 tras extract_competencia.
Uso:  ./.venv/bin/python competencia.py [consolidar|cambios|match_syscom]
      (default: ambos)
"""
import glob
import os
import re
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
CRUDOS = os.path.join(BASE, "data", "competencia")
DB = os.path.join(CRUDOS, "db")
UMBRAL_PRECIO = 0.01   # ±1% en su propia moneda = cambio real

COLS = ["fecha", "modelo", "marca", "categoria", "precio_lista",
        "descuento_pct", "precio_venta", "moneda", "existencia", "nombre"]

CATALOGO = os.path.expanduser("~/Downloads/cat_modelos.csv")
CORTES = [(0.75, "FUZZY_ALTO"), (0.60, "FUZZY_MEDIO")]
_SC = re.compile(r"[^A-Z0-9]")


def _tc_por_semana():
    """Tipo de cambio por semana desde el panel (mediana), para convertir
    MXN→USD (usuario 2026-08-07). Fallback: mediana de las últimas 4 semanas."""
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"),
                          columns=["semana", "tc"])
    tcs = pan.groupby("semana").tc.median()
    reciente = float(pan[pan.semana >= pan.semana.max()
                         - pd.Timedelta(weeks=4)].tc.median())
    return tcs, reciente


def consolidar():
    """CSV crudos → una BD parquet por competidor (idempotente)."""
    os.makedirs(DB, exist_ok=True)
    rutas = [r for r in glob.glob(os.path.join(CRUDOS, "*.csv"))
             if not os.path.basename(r).startswith("_")]
    por_fuente = {}
    for r in rutas:
        fuente = os.path.basename(r).rsplit("_", 1)[0]
        por_fuente.setdefault(fuente, []).append(r)
    for fuente, archivos in sorted(por_fuente.items()):
        partes = []
        for a in archivos:
            try:
                d = pd.read_csv(a)
            except Exception as e:
                print(f"  {os.path.basename(a)}: ilegible ({str(e)[:40]})", flush=True)
                continue
            d = d[[c for c in COLS if c in d.columns]].copy()
            partes.append(d)
        if not partes:
            continue
        df = pd.concat(partes, ignore_index=True)
        df["modelo"] = (df["modelo"].astype(str).str.upper().str.strip()
                        .str.replace("-", "", regex=False))
        df["fecha"] = pd.to_datetime(df.fecha).dt.date.astype(str)
        for c in ("precio_lista", "descuento_pct", "precio_venta", "existencia"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        # PRECIOS EN USD (usuario 2026-08-07): convertir cuando la moneda sea
        # MXN/MN/pesos, con el tipo de cambio de la SEMANA de cada dato (panel).
        # La detección de cambios usa la moneda NATIVA (el FX no es un cambio).
        tcs, tc_rec = _tc_por_semana()
        sem = pd.to_datetime(df.fecha).dt.to_period("W-SUN").dt.start_time
        tc_fila = sem.map(tcs).fillna(tc_rec)
        es_mxn = df.moneda.astype(str).str.upper().str.contains(
            "MXN|PESO|^MN$|M\\.N", regex=True, na=False)
        df["precio_venta_usd"] = np.where(es_mxn, df.precio_venta / tc_fila,
                                          df.precio_venta).round(2)
        df["precio_lista_usd"] = np.where(es_mxn, df.precio_lista / tc_fila,
                                          df.precio_lista).round(2)
        ruta_db = os.path.join(DB, f"{fuente}.parquet")
        if os.path.exists(ruta_db):
            df = pd.concat([pd.read_parquet(ruta_db), df], ignore_index=True)
        df = (df.sort_values("fecha")
              .drop_duplicates(["modelo", "fecha"], keep="last")
              .reset_index(drop=True))
        df.to_parquet(ruta_db, index=False)
        print(f"  {fuente:<14} {df.modelo.nunique():>7,} modelos | "
              f"{df.fecha.nunique():>3} fechas | {len(df):>9,} filas → {ruta_db}",
              flush=True)


def cambios():
    """Detecta movimientos por competidor comparando fechas consecutivas."""
    filas = []
    for ruta_db in sorted(glob.glob(os.path.join(DB, "*.parquet"))):
        fuente = os.path.basename(ruta_db)[:-8]
        df = pd.read_parquet(ruta_db).sort_values(["modelo", "fecha"])
        df["pv_prev"] = df.groupby("modelo").precio_venta.shift(1)
        df["ex_prev"] = df.groupby("modelo").existencia.shift(1)
        df["fecha_prev"] = df.groupby("modelo").fecha.shift(1)
        primera = df.fecha.min()
        for x in df.itertuples():
            if pd.isna(x.pv_prev):
                if x.fecha != primera:
                    filas.append((fuente, x.fecha, x.modelo, "ALTA", np.nan,
                                  x.precio_venta, x.moneda))
                continue
            if x.pv_prev > 0 and pd.notna(x.precio_venta):
                d_ = x.precio_venta / x.pv_prev - 1
                if abs(d_) >= UMBRAL_PRECIO:
                    filas.append((fuente, x.fecha, x.modelo,
                                  "PRECIO SUBIÓ" if d_ > 0 else "PRECIO BAJÓ",
                                  round(100 * d_, 1), x.precio_venta, x.moneda))
            if pd.notna(x.ex_prev) and pd.notna(x.existencia):
                if x.ex_prev > 0 and x.existencia == 0:
                    filas.append((fuente, x.fecha, x.modelo, "STOCKOUT",
                                  np.nan, x.precio_venta, x.moneda))
                elif x.ex_prev == 0 and x.existencia > 0:
                    filas.append((fuente, x.fecha, x.modelo, "REABASTECIDO",
                                  np.nan, x.precio_venta, x.moneda))
    C = pd.DataFrame(filas, columns=["fuente", "fecha", "modelo", "tipo",
                                     "delta_pct", "precio_venta", "moneda"])
    C.to_parquet(os.path.join(CRUDOS, "cambios.parquet"), index=False)
    corte = str(pd.Timestamp.today().date() - pd.Timedelta(days=7))
    C[C.fecha >= corte].to_csv(os.path.join(BASE, "out", "competencia_cambios.csv"),
                               index=False)
    print(f"\ncambios detectados (historia): {len(C):,}", flush=True)
    if len(C):
        print(C.groupby(["fuente", "tipo"]).size().unstack(fill_value=0).to_string(),
              flush=True)
    return C


def _norm(x):
    """Normaliza un código de modelo: MAYÚSCULAS, solo A-Z0-9."""
    return _SC.sub("", str(x).upper())


def _catalogo_syscom():
    """Catálogo SYSCOM (id_producto, modelo) normalizado, sin ruido
    (vacíos, longitud <2, códigos en notación científica)."""
    cat = pd.read_csv(CATALOGO, dtype={"modelo": str})
    cat = cat.dropna(subset=["modelo"]).copy()
    cat["modelo"] = cat.modelo.astype(str).str.strip()
    cat["m_norm"] = cat.modelo.map(_norm)
    cat = cat[(cat.m_norm.str.len() >= 2)
              & ~cat.m_norm.str.contains(r"E[+-]\d+$", regex=True)]
    return (cat.drop_duplicates("m_norm").reset_index(drop=True)
              [["modelo", "m_norm", "id_producto"]])


def match_syscom():
    """Capa 3: cada modelo de la competencia → su modelo SYSCOM más parecido.
    Estrategia de IA: match exacto normalizado primero; para los no-exactos,
    candidatos por TF-IDF de n-gramas de caracteres (2-4) con NearestNeighbors
    (coseno, top-K vectorizado) + re-rank con RapidFuzz. Los códigos de
    producto son secuencias alfanuméricas — la similitud de n-gramas de
    caracteres captura variantes (guiones, espacios, sufijos de especificación)
    mejor que texto semántico."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.neighbors import NearestNeighbors
    from rapidfuzz import fuzz

    cat = _catalogo_syscom()
    catalogo = list(cat.m_norm)
    idx_exacto = {c: i for i, c in enumerate(catalogo)}
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1)
    X = vec.fit_transform(catalogo)
    print(f"catálogo SYSCOM: {len(cat):,} modelos | "
          f"n-gramas: {len(vec.vocabulary_):,}", flush=True)
    nn = NearestNeighbors(n_neighbors=10, metric="cosine", algorithm="brute")
    nn.fit(X)

    # Únicos de la competencia (todos los competidores)
    unicos = {}
    for ruta in sorted(glob.glob(os.path.join(DB, "*.parquet"))):
        fu = os.path.basename(ruta)[:-8]
        df = pd.read_parquet(ruta, columns=["modelo"])
        unicos[fu] = sorted(df.modelo.dropna().astype(str).unique())
        del df
    total = sum(len(v) for v in unicos.values())
    print(f"modelos de competencia a emparejar: {total:,}", flush=True)

    filas, n_exacto, n_alto, n_medio, n_sin = [], 0, 0, 0, 0
    for fu, modelos in sorted(unicos.items()):
        norm = [_norm(m) for m in modelos]
        exactos = [mn in idx_exacto for mn in norm]
        # exactos
        for m, mn, ex in zip(modelos, norm, exactos):
            if ex:
                filas.append((fu, m, cat.modelo[idx_exacto[mn]],
                              mn, "EXACTO", 1.0))
                n_exacto += 1
        # no-exactos: top-K por NearestNeighbors + re-rank RapidFuzz
        resto = [mn for mn, ex in zip(norm, exactos) if not ex]
        if resto:
            Q = vec.transform(resto)
            dist, neigh = nn.kneighbors(Q)
            for mn, ds, ns in zip(resto, dist, neigh):
                mejor, mejor_s = None, -1.0
                for d, i in zip(ds, ns):
                    c = catalogo[i]
                    r = fuzz.ratio(mn, c) / 100.0
                    lr = min(len(mn), len(c)) / max(len(mn), len(c))
                    s = 0.50 * (1.0 - float(d)) + 0.30 * r + 0.20 * lr
                    if s > mejor_s:
                        mejor, mejor_s = c, s
                if mejor_s >= CORTES[0][0]:
                    filas.append((fu, mn, cat.modelo[cat.m_norm == mejor]
                                  .iloc[0], mejor, "FUZZY_ALTO",
                                  round(mejor_s, 3)))
                    n_alto += 1
                elif mejor_s >= CORTES[1][0]:
                    filas.append((fu, mn, cat.modelo[cat.m_norm == mejor]
                                  .iloc[0], mejor, "FUZZY_MEDIO",
                                  round(mejor_s, 3)))
                    n_medio += 1
                else:
                    filas.append((fu, mn, "", "", "SIN_COINCIDENCIA", 0.0))
                    n_sin += 1
    M = pd.DataFrame(filas, columns=["fuente", "modelo_comp", "modelo_syscom",
                                     "m_norm_syscom", "nivel", "score"])
    M.to_parquet(os.path.join(CRUDOS, "match_syscom.parquet"), index=False)
    rev = M[M.nivel.isin(["EXACTO", "FUZZY_ALTO", "FUZZY_MEDIO"])]
    rev.to_csv(os.path.join(BASE, "out", "competencia_match_syscom.csv"),
               index=False)
    print(f"\nmatching: {n_exacto:,} EXACTO | {n_alto:,} FUZZY_ALTO | "
          f"{n_medio:,} FUZZY_MEDIO | {n_sin:,} SIN_COINCIDENCIA",
          flush=True)
    print(f"  revisables (EXACTO/ALTO/MEDIO): {len(rev):,} → "
          f"out/competencia_match_syscom.csv", flush=True)
    return M


def _nombre_competencia():
    """nombre (texto del producto) más reciente por modelo de cada competidor.
    `match_syscom.modelo_comp` guarda el modelo normalizado (_norm); se une por
    la versión normalizada del modelo crudo de la BD. Devuelve
    (fuente, modelo_norm, nombre)."""
    partes = []
    for ruta in sorted(glob.glob(os.path.join(DB, "*.parquet"))):
        fu = os.path.basename(ruta)[:-8]
        d = pd.read_parquet(ruta, columns=["modelo", "nombre", "fecha"])
        d["fuente"] = fu
        partes.append(d)
    df = pd.concat(partes, ignore_index=True)
    df["modelo"] = df.modelo.astype(str).str.strip()
    df["m_norm"] = df.modelo.map(_norm)
    df["nombre"] = df.nombre.fillna("").astype(str).str.strip()
    ult = (df.sort_values("fecha")
             .drop_duplicates(["fuente", "m_norm"], keep="last"))
    return ult[["fuente", "m_norm", "modelo", "nombre"]]


def match_desc():
    """Capa 4: validación textual del matching Capa 3 sobre la zona ambigua.

    Para cada modelo de competencia (zona FUZZY_MEDIO / SIN_COINCIDENCIA)
    se usa `nombre` (texto del producto en el CSV del competidor) como query
    contra un índice TF-IDF (n-gramas de caracteres) de las descripciones
    del catálogo SYSCOM (`data/reporte61/catalogo_descripciones.parquet`).
    Devuelve top-5 candidatos SYSCOM por similitud textual; así se confirma
    (o corrige) el candidato que dio el match por código (Capa 3).

    Capa 1 (rápida, sin API): este método. Capa 2 (SBERT): se confirma con
    embeddings semánticos la zona que aquí deja duda (borde de score).

    Salida: data/competencia/match_desc.parquet +
            out/competencia_match_desc.csv (top-1 con score y descripción).
    """
    import html as _html
    import re as _re
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.neighbors import NearestNeighbors

    def _txt(s):
        """Texto limpio para el índice: minúsculas, solo a-z0-9 + espacio."""
        return _re.sub(r"[^a-z0-9]", " ", str(s).lower())

    cat = os.path.join(BASE, "data", "reporte61", "catalogo_descripciones.parquet")
    c = pd.read_parquet(cat, columns=["codigo", "descripcion", "marca"])
    c["codigo"] = c.codigo.astype(str).str.strip()
    c["desc"] = (c.descripcion.fillna("")
                 .map(lambda s: _html.unescape(str(s)))).str.lower()
    c = c[c.desc.str.strip().ne("")].drop_duplicates("codigo").reset_index(drop=True)

    # RESTRINGIR a códigos con venta ACTUAL (usuario 2026-08-07): no buscar el
    # top-1 en todo el catálogo (donde miles de códigos muertos compiten por el
    # mismo texto); se valida solo entre lo que se VENDE hoy.
    ruta_act = os.path.join(BASE, "data", "reporte61", "codigos_activos.parquet")
    if os.path.exists(ruta_act):
        act = set(pd.read_parquet(ruta_act)["codigo"].astype(str))
        antes = len(c)
        c = c[c.codigo.isin(act)].reset_index(drop=True)
        print(f"  restricción a venta actual: {antes:,} → {len(c):,} "
              f"descripciones", flush=True)
    print(f"descripciones SYSCOM indexables: {len(c):,}", flush=True)

    # n-gramas de CARACTERES: capturan códigos/fichas técnicas que comparte el
    # nombre del competidor con la descripción SYSCOM (mejor que palabras, que
    # matchean texto genérico; ver cand 0026134→cable en prueba word vs char).
    vec = TfidfVectorizer(analyzer="char", ngram_range=(3, 5), min_df=2)
    X = vec.fit_transform(c["desc"].map(_txt))
    print(f"n-gramas de caracteres: {len(vec.vocabulary_):,}", flush=True)
    nn = NearestNeighbors(n_neighbors=5, metric="cosine", algorithm="brute")
    nn.fit(X)

    M = pd.read_parquet(os.path.join(CRUDOS, "match_syscom.parquet"))
    if "m_norm_syscom" not in M.columns:
        M["m_norm_syscom"] = M.modelo_syscom.map(_norm)
    zona = M[M.nivel.isin(["FUZZY_MEDIO", "SIN_COINCIDENCIA"])].copy()
    nombres = _nombre_competencia()
    z = zona.merge(nombres[["fuente", "m_norm", "nombre"]],
                   left_on=["fuente", "modelo_comp"],
                   right_on=["fuente", "m_norm"], how="left")
    z = z[z.nombre.ne("")]
    print(f"modelos ambivalentes con nombre: {len(z):,}", flush=True)

    Q = vec.transform(z["nombre"].map(_txt))
    dist, neigh = nn.kneighbors(Q)
    cods = c["codigo"].to_numpy()
    filas, top5 = [], []
    for fu, mc, nvl, nombre, ds, ns in zip(z["fuente"], z["modelo_comp"],
                                            z["nivel"], z["nombre"],
                                            dist, neigh):
        tope = sorted(zip(ns, ds), key=lambda x: x[1])
        fila_t5 = (fu, mc, nvl)
        for r_, (i, d) in enumerate(tope, 1):
            cod = cods[i]
            des = c.loc[c["codigo"] == cod, "desc"].iloc[0]
            sim = round(1.0 - float(d) + 1e-12, 3)
            top5.append(fila_t5 + (r_, cod, des, sim))
            if r_ == 1:
                filas.append((fu, nvl, mc, cod, des, sim))
    TO5 = pd.DataFrame(top5, columns=["fuente", "modelo_comp", "nivel", "rank",
                                      "modelo_syscom", "descripcion_syscom",
                                      "sim_tfidf"])
    TO5.to_parquet(os.path.join(CRUDOS, "match_desc_top5.parquet"), index=False)
    R = pd.DataFrame(filas, columns=["fuente", "nivel", "modelo_comp",
                                     "modelo_syscom", "descripcion_syscom",
                                     "score_tfidf"])
    R.to_parquet(os.path.join(CRUDOS, "match_desc.parquet"), index=False)
    R.sort_values("score_tfidf").to_csv(
        os.path.join(BASE, "out", "competencia_match_desc.csv"), index=False)
    print(f"\nmatch_desc (Capa 4): {len(R):,} filas (top-5 en "
          f"match_desc_top5.parquet) → out/competencia_match_desc.csv",
          flush=True)
    return R


def match_desc_confirm():
    """Capa 5 (SBERT): confirma/reordena los candidatos de la Capa 4.

    Solo actúa sobre los modelos donde la Capa 4 dejó duda: top-1 con
    sim_tfidf < UMBRAL_SBERT (0.35). Para cada uno, codifica con SentenceTransformer
    el `nombre` del competidor y las descripciones de sus top-5 candidatos SYSCOM
    (los que ya seleccionó el fast-TF-IDF), y reordena por similitud senangular
    semántica (coseno). El resultado es el candidato SYSCOM que el modelo
    semántico juzga más parecido al texto del producto competidor.

    Modelo: paraphrase-multilingual-MiniLM-L12-v2 (español+inglés, caché local).
    Es DETERMINISTA y local; ~4K casos ≈ unos minutos.

    Salida: data/competencia/match_desc_sbert.parquet +
            out/competencia_match_desc_sbert.csv.
    """
    import html as _html
    import numpy as _np

    UMBRAL_D = 0.35
    top = os.path.join(CRUDOS, "match_desc_top5.parquet")
    t5 = pd.read_parquet(top)
    t1 = t5[t5["rank"] == 1].copy()
    dudosos = t1[t1.sim_tfidf < UMBRAL_D]
    print(f"Capa 5: {len(dudosos):,} modelos con sim_tfidf<{UMBRAL_D} "
          f"(de {len(t1):,})", flush=True)
    if not len(dudosos):
        print("  nada que confirmar", flush=True)
        return t5

    # unir nombre del competidor
    nombres = _nombre_competencia()
    d = dudosos.merge(nombres[["fuente", "m_norm", "nombre"]],
                      left_on=["fuente", "modelo_comp"],
                      right_on=["fuente", "m_norm"], how="left")
    d = d[d.nombre.ne("")].reset_index(drop=True)
    print(f"  con nombre: {len(d):,}", flush=True)

    from sentence_transformers import SentenceTransformer
    mdl = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    refs = {c: i for i, c in enumerate(t5.modelo_syscom.unique())}
    # (refs sin uso; se conserva la estructura del índice para futura búsqueda amplia)
    filas = []
    for r in d.itertuples(index=False):
        sub = t5[(t5.fuente == r.fuente) & (t5.modelo_comp == r.modelo_comp)]
        embs = mdl.encode([r.nombre] + list(sub.descripcion_syscom),
                          normalize_embeddings=True, convert_to_numpy=True)
        sims = _np.dot(embs[1:], embs[0])
        mejores = sorted(zip(sub["modelo_syscom"], sub["descripcion_syscom"], sims),
                         key=lambda x: -float(x[2]))
        ms, des, sbest = mejores[0]
        filas.append((r.fuente, r.nivel, r.modelo_comp, r.nombre,
                      ms, des, round(float(sbest), 3), r.sim_tfidf))
    out = pd.DataFrame(filas, columns=["fuente", "nivel", "modelo_comp", "nombre",
                                     "modelo_syscom_sbert", "descripcion_syscom_sbert",
                                     "sim_sbert", "sim_tfidf"])
    out.to_parquet(os.path.join(CRUDOS, "match_desc_sbert.parquet"), index=False)
    out.sort_values("sim_sbert").to_csv(
        os.path.join(BASE, "out", "competencia_match_desc_sbert.csv"), index=False)
    print(f"\nmatch_desc_confirm (Capa 5): {len(out):,} confirmados → "
          f"out/competencia_match_desc_sbert.csv", flush=True)
    return out


def actualizar():
    """Paso del cron diario: consolidar lo nuevo + re-detectar cambios."""
    consolidar()
    cambios()


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    {"consolidar": consolidar, "cambios": cambios,
     "match_syscom": match_syscom, "match_desc": match_desc,
     "match_desc_confirm": match_desc_confirm}.get(modo, actualizar)()
