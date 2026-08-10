# -*- coding: utf-8 -*-
"""DUELO DE RANKING SEMÁNTICO para equivalentes entre marcas (2026-08-10).

CAMPEÓN:  MiniLM (bi-encoder local actual) — coseno dentro del bloque.
RETADOR:  BAAI/bge-reranker-v2-m3 (cross-encoder local, gratuito, sustituto
          estándar de Cohere Rerank; ningún dato sale de la máquina).

JUEZ FIJADO ANTES DE CORRER: cada brazo elige su candidato SOLO por señal
semántica (el precio NO participa en el ranking) dentro del MISMO bloque de
atributos; el veredicto lo da la FIRMA DE PRECIO como verdad externa
(validada: los sustitutos reales tienen gap mediano ~0.2%):
  J1 = mediana de |gap%| del candidato elegido   (menor = mejor)
  J2 = % de elecciones con |gap| ≤ 25%           (mayor = mejor)
ADOPCIÓN: el retador entra solo si gana en AMBOS jueces; empate ⇒ campeón
(menor dependencia). Población: carril VECTOR con ≥2 candidatos en el bloque.
"""
import os

import numpy as np
import pandas as pd

from equivalentes import RIVALES, _attr, _norm_marca

BASE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.join(BASE, "data", "competencia")


def correr():
    import glob
    desc = pd.read_parquet(os.path.join(BASE, "data", "reporte61",
                                        "catalogo_descripciones.parquet"))
    act = pd.read_parquet(os.path.join(BASE, "data", "reporte61",
                                       "codigos_activos.parquet"))
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"),
                          columns=["codigo", "semana", "neto_prom"])
    ult = pan.sort_values("semana").drop_duplicates("codigo", keep="last")
    S = (desc.merge(act, on="codigo").merge(ult[["codigo", "neto_prom"]], on="codigo"))
    S = S[S.neto_prom > 0].copy()
    S["marca_n"] = S.marca.map(_norm_marca)
    S[["tipo", "mp", "mm", "tec", "gama"]] = [(_attr(d)) for d in S.descripcion]
    S = S.dropna(subset=["tipo", "mp"]).reset_index(drop=True)

    partes = []
    for f in glob.glob(os.path.join(COMP, "db", "*.parquet")):
        d = pd.read_parquet(f)
        if "nombre" not in d.columns:
            continue
        d["fuente"] = os.path.basename(f)[:-8]
        partes.append(d.sort_values("fecha").drop_duplicates(["fuente", "modelo"], keep="last"))
    C = pd.concat(partes, ignore_index=True)
    C = C[(C.precio_venta_usd > 0) & C.nombre.notna()].copy()
    # identidades fuera (misma regla que equivalentes)
    mx = pd.read_parquet(os.path.join(COMP, "syscom_vs_distribuidores.parquet"))
    exactos = set(zip(mx[mx.nivel == "EXACTO"].distribuidor,
                      mx[mx.nivel == "EXACTO"].modelo_distribuidor))
    C = C[[(f_, m_) not in exactos for f_, m_ in zip(C.fuente, C.modelo)]]
    C["marca_n"] = C.marca.map(_norm_marca)
    C[["tipo", "mp", "mm", "tec", "gama"]] = [(_attr(n)) for n in C.nombre]
    C = C.dropna(subset=["tipo", "mp"]).reset_index(drop=True)

    # bloques del carril VECTOR con ≥2 candidatos (donde el ranking decide)
    S_idx = {k: g.index for k, g in S.groupby(["tipo", "mp", "tec"])}
    casos = []
    for i, x in enumerate(C.itertuples()):
        idx = S_idx.get((x.tipo, x.mp, x.tec))
        if idx is None:
            continue
        g = S.loc[idx]
        g = g[(g.marca_n != x.marca_n) & (g.gama == x.gama)]
        if x.mm is not None:
            g = g[(g.mm.notna()) & ((g.mm - x.mm).abs() <= 1.2)]
        if len(g) >= 2:
            casos.append((i, list(g.index)))
    print(f"población del duelo: {len(casos)} productos con ≥2 candidatos", flush=True)

    # CAMPEÓN: MiniLM bi-encoder (solo semántica)
    from sentence_transformers import SentenceTransformer, CrossEncoder
    st = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    emb_s = st.encode(S.descripcion.astype(str).tolist(), normalize_embeddings=True,
                      show_progress_bar=False)
    emb_c = st.encode(C.nombre.astype(str).tolist(), normalize_embeddings=True,
                      show_progress_bar=False)
    # RETADOR: cross-encoder BGE (gratuito, local)
    ce = CrossEncoder("BAAI/bge-reranker-v2-m3")

    res = []
    for i, cand in casos:
        q = str(C.nombre.iloc[i])
        sims = emb_s[cand] @ emb_c[i]
        pick_c = cand[int(np.argmax(sims))]
        scores = ce.predict([(q, str(S.descripcion.loc[j])) for j in cand])
        pick_r = cand[int(np.argmax(scores))]
        pv = float(C.precio_venta_usd.iloc[i])
        gap = lambda j: abs(100 * (pv / float(S.neto_prom.loc[j]) - 1))
        res.append((gap(pick_c), gap(pick_r), pick_c == pick_r))
    R = pd.DataFrame(res, columns=["gap_campeon", "gap_retador", "coinciden"])
    R.to_parquet(os.path.join(COMP, "duel_rerank.parquet"), index=False)

    j1c, j1r = R.gap_campeon.median(), R.gap_retador.median()
    j2c = (R.gap_campeon <= 25).mean()
    j2r = (R.gap_retador <= 25).mean()
    print(f"\n== VEREDICTO (juez = firma de precio, NO usada en el ranking) ==", flush=True)
    print(f"  J1 |gap| mediano:  campeón MiniLM {j1c:.1f}%  vs  retador BGE {j1r:.1f}%", flush=True)
    print(f"  J2 |gap|≤25%:      campeón {j2c:.0%}  vs  retador {j2r:.0%}", flush=True)
    print(f"  coinciden en la elección: {R.coinciden.mean():.0%} de {len(R)}", flush=True)
    gana = j1r < j1c and j2r > j2c
    print(f"  ⇒ {'RETADOR ENTRA (gana ambos jueces)' if gana else 'CAMPEÓN se queda (regla: ganar AMBOS o no entra)'}",
          flush=True)


if __name__ == "__main__":
    correr()
