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
    mp = re.search(r"(\d+(?:\.\d+)?)\s*(?:mp\b|megapix)", t)
    mm = re.search(r"(\d+(?:\.\d+)?)\s*mm\b", t)
    tec = next((n for n, p in TEC if re.search(p, t)), None)
    return (tipo, float(mp.group(1)) if mp else None,
            float(mm.group(1)) if mm else None, tec)


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
    S[["tipo", "mp", "mm", "tec"]] = [(_attr(d)) for d in S.descripcion]
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
    C[["tipo", "mp", "mm", "tec"]] = [(_attr(n)) for n in C.nombre]
    C = C.dropna(subset=["tipo", "mp"])
    print(f"competencia con atributos: {len(C):,}", flush=True)

    # BLOQUEO: mismo tipo+mp+tec, lente ±0.4mm, marca RIVAL
    pares = []
    S_idx = {k: g for k, g in S.groupby(["tipo", "mp", "tec"])}
    for x in C.itertuples():
        riv = RIVALES.get(x.marca_n)
        if not riv:
            continue
        g = S_idx.get((x.tipo, x.mp, x.tec))
        if g is None:
            continue
        g2 = g[g.marca_n.isin(riv)]
        if x.mm is not None:
            g2 = g2[(g2.mm.notna()) & ((g2.mm - x.mm).abs() <= 0.4)]
        if g2.empty:
            continue
        # ranking por cercanía de PRECIO (sustitutos se parecen hasta en precio)
        cerc = (np.log(g2.neto_prom / x.precio_venta_usd)).abs()
        mejor = g2.loc[cerc.idxmin()]
        pares.append({
            "fuente": x.fuente, "modelo_comp": x.modelo, "marca_comp": x.marca_n,
            "nombre_comp": str(x.nombre)[:80], "precio_comp_usd": round(x.precio_venta_usd, 2),
            "codigo_syscom": mejor.codigo, "marca_syscom": mejor.marca_n,
            "descripcion_syscom": str(mejor.descripcion)[:80],
            "subtotal_syscom": round(float(mejor.neto_prom), 2),
            "tipo": x.tipo, "mp": x.mp, "mm": x.mm, "tec": x.tec,
            "gap_pct": round(100 * (x.precio_venta_usd / float(mejor.neto_prom) - 1), 1),
            "cercania_precio": round(float(cerc.min()), 3),
        })
    E = pd.DataFrame(pares)
    if len(E):
        E["nivel"] = "EQUIVALENTE"   # JAMÁS se mezcla con EXACTO
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
