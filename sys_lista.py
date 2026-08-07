# -*- coding: utf-8 -*-
"""Listado SYSCOM -> competidores (match por modelo y por texto, completo).

Une la Capa 3 (match por modelo: EXACTO/FUZZY_ALTO/FUZZY_MEDIO) con el maestro
de texto (Capa 4 TF-IDF + Capa 5 SBERT) y produce la lista final de pares
(distribuidor, modelo_distribuidor) -> modelo_syscom con candidato/via/score.

Prioridad del candidato final por (distribuidor, modelo_distribuidor):
  1) EXACTO por modelo      -> ese modelo (via MODELO)
  2) texto (TFIDF o SBERT)  -> el que trae el maestro (zona ambigua)
  3) FUZZY_ALTO / FUZZY_MEDIO por modelo como respaldo

Salida:
  data/competencia/syscom_vs_distribuidores.parquet
  out/syscom_vs_distribuidores.csv
"""
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.join(BASE, "data", "competencia")

m3 = pd.read_parquet(os.path.join(COMP, "match_syscom.parquet"))
maestro = pd.read_parquet(os.path.join(COMP, "match_desc_maestro.parquet"))
cd = pd.read_parquet(os.path.join(BASE, "data", "reporte61",
                                  "catalogo_descripciones.parquet"))[
    ["codigo", "descripcion", "marca"]]
cd["codigo"] = cd.codigo.astype(str).str.strip()

# --- Capa 3 por modelo (EXACTO / ALTO / MEDIO) ---
m3k = m3[m3.nivel.isin(["EXACTO", "FUZZY_ALTO", "FUZZY_MEDIO"])].copy()
m3k = m3k.rename(columns={"nivel": "nivel_modelo", "score": "score_modelo"})
m3k["prioridad"] = np.where(m3k.nivel_modelo == "EXACTO", 1,
                    np.where(m3k.nivel_modelo == "FUZZY_ALTO", 2, 3))
m3k = m3k[["fuente", "modelo_comp", "modelo_syscom", "nivel_modelo",
           "score_modelo", "prioridad"]]

# --- Maestro por texto (TFIDF / SBERT), solo los que aportan candidato ---
txt = maestro.rename(columns={"nivel": "nivel_texto", "score_tfidf": "score_txt"})
txt = txt[["fuente", "modelo_comp", "via", "modelo_syscom", "score_txt"]]

# --- Merge outer y candidato final ---
j = m3k.merge(txt, on=["fuente", "modelo_comp"], how="outer",
              suffixes=("", "_txt"))
j["modelo_syscom"] = np.where(pd.notna(j["modelo_syscom_txt"]),
                              j["modelo_syscom_txt"], j["modelo_syscom"])
j["via_final"] = np.where(pd.notna(j["via"]), j["via"], "MODELO")
j["score_final"] = np.where(pd.notna(j["score_txt"]),
                            j["score_txt"], j["score_modelo"])
j["nivel_final"] = np.where(pd.isna(j["nivel_modelo"]), "TEXTO",
                            j["nivel_modelo"])
j = j.dropna(subset=["modelo_syscom"])
# quitar duplicados (modelo_comp puede estar en m3 y maestro a la vez)
j = (j.sort_values(["modelo_comp", "score_final"], ascending=[True, False])
      .drop_duplicates(["fuente", "modelo_comp"]))
# filtrar filas sin candidato real
j = j[j.modelo_syscom.astype(str).str.strip().ne("")]

out = j[["fuente", "modelo_comp", "modelo_syscom", "via_final", "score_final",
         "nivel_final"]].copy()
out["modelo_syscom"] = out.modelo_syscom.astype(str)

out = out.merge(cd, left_on="modelo_syscom", right_on="codigo", how="left")
out = out.drop(columns=["codigo"])

out = out.rename(columns={"fuente": "distribuidor", "modelo_comp": "modelo_distribuidor",
                          "descripcion": "descripcion_syscom", "nivel_final": "nivel"})
out = out.sort_values(["modelo_syscom", "distribuidor"])

out.to_parquet(os.path.join(COMP, "syscom_vs_distribuidores.parquet"), index=False)
out.to_csv(os.path.join(BASE, "out", "syscom_vs_distribuidores.csv"), index=False)

print(f"rows: {len(out):,}")
print(f"SYSCOM únicos con match: {out.modelo_syscom.nunique():,} "
      f"de {cd.shape[0]:,} en catálogo")
print(out.groupby("via_final").size().to_string())
print("\nEjemplo:")
print(out[out.modelo_syscom.str.contains("NK5EPC10", na=False)].head(4).to_string())