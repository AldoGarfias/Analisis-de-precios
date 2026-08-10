# -*- coding: utf-8 -*-
"""Análisis: precio SYSCOM (lista y subtotal/neto) vs competencia (lista y venta USD),
solo sobre pares EXACTO del match. Salida: out/precios_vs_competencia.csv + .parquet.
"""
import os

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.join(BASE, "data", "competencia")

# 1) pares exactos (SYSC vs distribuidor)
o = pd.read_parquet(os.path.join(COMP, "syscom_vs_distribuidores.parquet"))
ex = o[(o.via_final == "MODELO") & (o.nivel == "EXACTO")].copy()
print(f"pares EXACTO: {len(ex):,}", flush=True)

# 2) nuestro panel: precio_lista y neto_prom recientes (YA EN USD, misma escala
#    que el extract: precio/subtotal/costo son USD importación; el tc es para
#    facturar MXN, no para convertir el precio). NO se divide por tc.
pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
pan = pan.sort_values("semana")
ult = pan.drop_duplicates("codigo", keep="last").copy()
ult = ult[["codigo", "semana", "neto_prom", "precio_lista", "tc", "tipo_precio", "margen"]]
ult["precio_lista_usd_sys"] = ult.precio_lista.round(2)
ult["subtotal_usd_sys"] = ult.neto_prom.round(2)
ex = ex.merge(ult[["codigo", "semana", "precio_lista_usd_sys", "subtotal_usd_sys",
                   "precio_lista", "neto_prom", "tipo_precio"]],
              left_on="modelo_syscom", right_on="codigo", how="left").drop(columns=["codigo"])
print(f"con panel SYSCOM: {ex.precio_lista_usd_sys.notna().sum():,}", flush=True)

# 3) precio del distribuidor (más reciente por modelo_distribuidor+fuente)
import glob
pares = []
for ruta in sorted(glob.glob(os.path.join(COMP, "db", "*.parquet"))):
    fu = os.path.basename(ruta)[:-8]
    d = pd.read_parquet(ruta, columns=["modelo", "fecha", "precio_venta_usd",
                                       "precio_lista_usd", "existencia"])
    d = d.sort_values("fecha").drop_duplicates("modelo", keep="last")
    d = d.rename(columns={"modelo": "modelo_distribuidor", "fecha": "fecha_dist"})
    pares.append(d.assign(distribuidor=fu))
comp = pd.concat(pares, ignore_index=True)
ex = ex.merge(comp[["distribuidor", "modelo_distribuidor", "fecha_dist",
                    "precio_venta_usd", "precio_lista_usd"]],
              on=["distribuidor", "modelo_distribuidor"], how="left")

# 4) diferencias
ex["d_lista_usd"] = (ex.precio_venta_usd - ex.precio_lista_usd_sys).round(2)
ex["diff_pct_lista"] = (ex.precio_venta_usd / ex.precio_lista_usd_sys - 1).round(1)
ex["diff_pct_subtotal"] = (ex.precio_venta_usd / ex.subtotal_usd_sys - 1).round(1)
ex["ganador"] = ""

completed = ex
os.makedirs(os.path.join(BASE, "out"), exist_ok=True)
out = ex.sort_values(["modelo_syscom", "distribuidor"])
out.to_parquet(os.path.join(COMP, "analisis_exactos.parquet"), index=False)
out.to_csv(os.path.join(BASE, "out", "analisis_vs_exactos.csv"), index=False)

with_sys = out[out.precio_lista_usd_sys.notna()]
with_all = with_sys[with_sys.precio_venta_usd.notna()]
print(f"\npares EXACTO con precio SYS: {len(with_sys):,} | además con precio dist: {len(with_all):,}")
print(out.groupby("tipo_precio").size().head().to_string())