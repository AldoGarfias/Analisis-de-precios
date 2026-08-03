# -*- coding: utf-8 -*-
"""Clusters de similares por SKU (para el prior jerárquico y la elasticidad cruzada).

Determinístico y robusto (no depende de servicios externos):
  1) Componentes conexas del grafo de `cat_sustitutos` (sustitutos curados).
  2) SKUs sin sustituto -> bucket marca × linea × tramo de precio (cuartil).
Además define, por SKU, el `id_sust_cercano` = sustituto con precio de lista más
parecido (para el término cross-price del panel).

Salida: data/elast/clusters.parquet (id_art, cluster_id, id_sust_cercano).

Nota: el endpoint Vectorize `buscar-productos-similares-batch` (ERP_API_URL/
ERP_API_KEY) podría enriquecer la cobertura de vecinos; se deja como mejora
futura opt-in para no volver frágil el pipeline con una dependencia de red.
"""
import os

import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "elast")


class UnionFind:
    def __init__(self):
        self.padre = {}

    def find(self, x):
        self.padre.setdefault(x, x)
        while self.padre[x] != x:
            self.padre[x] = self.padre[self.padre[x]]
            x = self.padre[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.padre[ra] = rb


def _tramo_precio(df):
    """Cuartil de precio_1 dentro de cada (marca, linea); robusto a grupos chicos."""
    def q(s):
        try:
            return pd.qcut(s.rank(method="first"), 4, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series(0, index=s.index)
    return df.groupby(["marca", "linea"])["precio_1"].transform(q).fillna(0).astype(int)


def construir():
    art = pd.read_parquet(f"{DATA}/articulos.parquet")
    sust = pd.read_parquet(f"{DATA}/sustitutos.parquet")

    arts_validos = set(art["id_art"].astype(int))
    # 1) componentes conexas de sustitutos (solo aristas dentro del universo)
    uf = UnionFind()
    pares = sust[["id_art", "id_sust"]].dropna().astype(int)
    for a, b in pares.itertuples(index=False):
        if a in arts_validos and b in arts_validos:
            uf.union(a, b)
    comp = {a: uf.find(a) for a in arts_validos if a in uf.padre}

    # 2) bucket marca×linea×tramo para el resto
    art = art.copy()
    art["precio_1"] = pd.to_numeric(art["precio_1"], errors="coerce").fillna(0.0)
    art["tramo"] = _tramo_precio(art)
    art["bucket"] = (art["marca"].astype(str) + "|" + art["linea"].astype(str)
                     + "|t" + art["tramo"].astype(str))

    def cluster_de(row):
        a = int(row["id_art"])
        if a in comp:
            return f"sust:{comp[a]}"
        return f"cat:{row['bucket']}"

    art["cluster_id"] = art.apply(cluster_de, axis=1)

    # sustituto de precio más cercano (para cross-price)
    precio = dict(zip(art["id_art"].astype(int), art["precio_1"]))
    cand = {}
    for a, b in pares.itertuples(index=False):
        if a in arts_validos and b in arts_validos:
            cand.setdefault(a, []).append(b)
            cand.setdefault(b, []).append(a)

    def sust_cercano(a):
        opts = cand.get(a)
        if not opts:
            return np.nan
        pa = precio.get(a, 0.0)
        return min(opts, key=lambda o: abs(precio.get(o, 0.0) - pa))

    art["id_sust_cercano"] = art["id_art"].astype(int).map(sust_cercano)

    out = art[["id_art", "cluster_id", "id_sust_cercano"]].copy()
    out.to_parquet(f"{DATA}/clusters.parquet", index=False)
    n_sust = (out["cluster_id"].str.startswith("sust:")).sum()
    print(f"clusters.parquet: {len(out)} SKUs, {out['cluster_id'].nunique()} clusters "
          f"({n_sust} vía sustitutos, {out['id_sust_cercano'].notna().sum()} con sust cercano)",
          flush=True)
    return out


if __name__ == "__main__":
    construir()
