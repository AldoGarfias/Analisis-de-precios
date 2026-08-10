# -*- coding: utf-8 -*-
"""EQUIVALENTES ENTRE MARCAS RIVALES (usuario 2026-08-10): la competencia no
solo vende NUESTROS productos — vende marcas que compiten de frente
(Hikvision↔Dahua, Saxxon↔LinkedPro/Linan). Esta capa encuentra el producto
SUSTITUTO: "TVC vende una cámara domo 2.8mm 2MP de Dahua más barata que la
domo equivalente Hikvision de SYSCOM".

DISEÑO (distinto al matching de identidad — aquí la familia es lo que importa):
  1. ATRIBUTOS DUROS extraídos por regex de las descripciones de ambos lados:
     tipo (domo/bala/turret/ptz/nvr/dvr/switch), megapíxeles, lente mm,
     tecnología (ip/turbohd/hdcvi), canales/puertos.
  2. BLOQUEO: solo se comparan productos con MISMO tipo + MISMOS MP +
     lente ±0.4mm + misma tecnología, de marca RIVAL (mapa explícito).
  3. RANKING por cercanía de PRECIO (|log ratio|): los sustitutos reales se
     parecen hasta en precio (idea del usuario); empates → menor gap.

Etiqueta SIEMPRE "EQUIVALENTE" (jamás se mezcla con los pares EXACTOS de
identidad). Salida: data/competencia/equivalentes.parquet +
out/competencia_equivalentes.csv. Piloto: cámaras (la categoría más grande).
"""
import glob
import os
import re

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.join(BASE, "data", "competencia")

# mapa de marcas RIVALES (semilla del usuario 2026-08-10; ampliable)
RIVALES = {
    "HIKVISION": {"DAHUA", "IMOU", "EZVIZ", "HILOOK"},
    "DAHUA": {"HIKVISION", "HILOOK", "EPCOM", "EZVIZ"},
    "SAXXON": {"LINKEDPRO", "LINAN"},
    "LINKEDPRO": {"SAXXON", "UGREEN"},
    "LINAN": {"SAXXON"},
    "EPCOM": {"DAHUA", "HIKVISION"},
    "HILOOK": {"DAHUA", "IMOU"},
}

TIPOS = [("domo", r"\bdomo\b"), ("bala", r"\bbala\b|\bbullet\b"),
         ("turret", r"\bturret\b|\beyeball\b"), ("ptz", r"\bptz\b"),
         ("nvr", r"\bnvr\b"), ("dvr", r"\bdvr\b|\bxvr\b")]
TEC = [("turbohd", r"turbo\s*hd|turbohd"), ("hdcvi", r"hdcvi|cvi\b"),
       ("ip", r"\bip\b|\bpoe\b")]


def _attr(txt):
    """Atributos duros de una descripción (minúsculas)."""
    t = str(txt).lower()
    tipo = next((n for n, p in TIPOS if re.search(p, t)), None)
    gama = bool(re.search(r"anticorrosi|antiexplosi|explosion[- ]?proof|motoriz|lente mot", t))
    mp = re.search(r"(\d+(?:\.\d+)?)\s*(?:mp\b|megapix)", t)
    mm = re.search(r"(\d+(?:\.\d+)?)\s*mm\b", t)
    tec = next((n for n, p in TEC if re.search(p, t)), None)
    return (tipo, float(mp.group(1)) if mp else None,
            float(mm.group(1)) if mm else None, tec, gama)


def _norm_marca(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def correr():
    # lado SYSCOM: descripciones + marca + precio (subtotal) de ACTIVOS
    desc = pd.read_parquet(os.path.join(BASE, "data", "reporte61",
                                        "catalogo_descripciones.parquet"))
    act = pd.read_parquet(os.path.join(BASE, "data", "reporte61",
                                       "codigos_activos.parquet"))
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"),
                          columns=["codigo", "semana", "neto_prom", "precio_lista"])
    ult = pan.sort_values("semana").drop_duplicates("codigo", keep="last")
    S = (desc.merge(act, on="codigo")
         .merge(ult[["codigo", "neto_prom", "precio_lista"]], on="codigo"))
    S = S[S.neto_prom > 0].copy()
    S["marca_n"] = S.marca.map(_norm_marca)
    S[["tipo", "mp", "mm", "tec", "gama"]] = [(_attr(d)) for d in S.descripcion]
    S = S.dropna(subset=["tipo", "mp"])
    print(f"SYSCOM activos con atributos: {len(S):,} "
          f"({S.tipo.value_counts().to_dict()})", flush=True)

    # lado competencia: último registro por modelo con nombre + marca + USD
    partes = []
    for f in glob.glob(os.path.join(COMP, "db", "*.parquet")):
        d = pd.read_parquet(f)
        if "nombre" not in d.columns:
            continue
        d["fuente"] = os.path.basename(f)[:-8]
        partes.append(d.sort_values("fecha")
                      .drop_duplicates(["fuente", "modelo"], keep="last"))
    C = pd.concat(partes, ignore_index=True)
    C = C[(C.precio_venta_usd > 0) & C.nombre.notna()].copy()
    C["marca_n"] = C.marca.map(_norm_marca)
    C[["tipo", "mp", "mm", "tec", "gama"]] = [(_attr(n)) for n in C.nombre]
    C = C.dropna(subset=["tipo", "mp"])
    print(f"competencia con atributos: {len(C):,}", flush=True)

    # EMBEDDINGS (una sola vez, ~2.3K textos): el vector solo RANKEA dentro
    # del bloque de atributos — jamás matchea en catálogo abierto (medido:
    # 2% de precisión suelto; dentro del bloque es su zona honesta)
    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    S = S.reset_index(drop=True)
    C = C.reset_index(drop=True)
    emb_s = st.encode(S.descripcion.astype(str).tolist(), normalize_embeddings=True,
                      show_progress_bar=False)
    emb_c = st.encode(C.nombre.astype(str).tolist(), normalize_embeddings=True,
                      show_progress_bar=False)

    # DOS CARRILES (usuario 2026-08-10): RIVAL (mapa explícito, alta confianza)
    # y VECTOR (abierto a cualquier marca distinta: descripción vectorizada +
    # cercanía de SUBTOTAL como doble firma de seguridad)
    pares = []
    S_idx = {k: g.index for k, g in S.groupby(["tipo", "mp", "tec"])}
    for i, x in enumerate(C.itertuples()):
        idx = S_idx.get((x.tipo, x.mp, x.tec))
        if idx is None:
            continue
        g = S.loc[idx]
        riv = RIVALES.get(x.marca_n, set())
        via = "RIVAL" if len(g[g.marca_n.isin(riv)]) else "VECTOR"
        g2 = g[g.marca_n.isin(riv)] if via == "RIVAL" else g[g.marca_n != x.marca_n]
        # gama debe COINCIDIR (anticorrosión/motorizado no sustituye estándar)
        g2 = g2[g2.gama == x.gama]
        if x.mm is not None:
            # lentes estándar adyacentes (2.8/3.6/4) SÍ son sustitutos:
            # tolerancia un paso (≤1.2mm); la cercanía exacta se premia abajo
            g2 = g2[(g2.mm.notna()) & ((g2.mm - x.mm).abs() <= 1.2)]
        if g2.empty:
            continue
        # ranking: cercanía de PRECIO + lente; en carril VECTOR se suma la
        # similitud SEMÁNTICA de la descripción (doble firma precio+vector)
        cerc = ((np.log(g2.neto_prom / x.precio_venta_usd)).abs()
                + (0.10 * (g2.mm - x.mm).abs().fillna(0) if x.mm is not None else 0))
        if via == "VECTOR":
            sim = emb_s[g2.index] @ emb_c[i]
            cerc = cerc + (1.0 - sim)          # menor = mejor en ambas firmas
        mejor = g2.loc[cerc.idxmin()]
        sim_mejor = float(emb_s[cerc.idxmin()] @ emb_c[i])
        pares.append({
            "fuente": x.fuente, "modelo_comp": x.modelo, "marca_comp": x.marca_n,
            "nombre_comp": str(x.nombre)[:80], "precio_comp_usd": round(x.precio_venta_usd, 2),
            "codigo_syscom": mejor.codigo, "marca_syscom": mejor.marca_n,
            "descripcion_syscom": str(mejor.descripcion)[:80],
            "subtotal_syscom": round(float(mejor.neto_prom), 2),
            "tipo": x.tipo, "mp": x.mp, "mm": x.mm, "tec": x.tec,
            "gap_pct": round(100 * (x.precio_venta_usd / float(mejor.neto_prom) - 1), 1),
            "cercania_precio": round(float(cerc.min()), 3),
            "via": via, "sim_vector": round(sim_mejor, 3),
        })
    E = pd.DataFrame(pares)
    if len(E):
        # |gap|>60% = gamas distintas ⇒ DUDOSO; el carril VECTOR además exige
        # similitud semántica ≥0.55 (doble firma: si el vector o el precio
        # dudan, el par no es dato firme)
        E["nivel"] = np.where(E.gap_pct.abs() > 60, "DUDOSO",
                     np.where((E.via == "VECTOR") & (E.sim_vector < 0.55),
                              "DUDOSO", "EQUIVALENTE"))
        E = E.sort_values("cercania_precio")
    E.to_parquet(os.path.join(COMP, "equivalentes.parquet"), index=False)
    E.to_csv(os.path.join(BASE, "out", "competencia_equivalentes.csv"), index=False)
    print(f"\nEQUIVALENTES entre marcas rivales: {len(E):,} pares", flush=True)
    if len(E):
        amen = E[E.gap_pct < -10]
        print(f"  con la marca rival >10% más BARATA que nuestro equivalente: "
              f"{len(amen):,}", flush=True)
        print(f"  por tipo: {E.tipo.value_counts().to_dict()}", flush=True)
    return E


if __name__ == "__main__":
    correr()
