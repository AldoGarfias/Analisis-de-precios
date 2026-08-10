# -*- coding: utf-8 -*-
"""Listado comparativa SYSCOM vs competencia (EXACTOS) ordenado por venta SYSCOM."""
import os

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))

a = pd.read_parquet(os.path.join(BASE, "data/competencia", "analisis_precios.parquet"))
if "vs_lista" not in a.columns:
    a["vs_lista"] = (a.precio_venta_usd / a.precio_lista_usd_sys - 1) * 100
    a["vs_sub"] = (a.precio_venta_usd / a.subtotal_usd_sys - 1) * 100

# venta SYSCOM por código
pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
pan["sem_ts"] = pd.to_datetime(pan.semana)
ult = pan.sem_ts.max()
rec = pan[pan.sem_ts >= ult - pd.Timedelta(weeks=8)]
vtotal = pan.groupby("codigo").unidades.sum().rename("unidades_total")
v8 = rec.groupby("codigo").unidades.sum().rename("unidades_8s")
venta = vtotal.to_frame().join(v8).fillna(0).reset_index()

m = a.merge(venta, left_on="modelo_syscom", right_on="codigo", how="left").drop(columns=["codigo"])
m["unidades_total"] = m.unidades_total.fillna(0).astype(int)
m["unidades_8s"] = m.unidades_8s.fillna(0).astype(int)

g = (m.groupby("modelo_syscom")
       .agg(descripcion_syscom=("descripcion_syscom", "first"),
            marca=("marca", "first"),
            unidades_total=("unidades_total", "sum"),
            unidades_8s=("unidades_8s", "sum"),
            n_distribuidores=("distribuidor", "nunique"),
            precio_lista_sys=("precio_lista_usd_sys", "max"),
            subtotal_sys=("subtotal_usd_sys", "max"),
            pv_comp_prom=("precio_venta_usd", "mean"),
            pv_comp_min=("precio_venta_usd", "min"),
            pv_comp_med=("precio_venta_usd", "median"))
       .reset_index())
g["vs_lista_med"] = ((g.pv_comp_med / g.precio_lista_sys) - 1) * 100
g["vs_sub_med"] = ((g.pv_comp_med / g.subtotal_sys) - 1) * 100
g = g.sort_values(["unidades_8s", "unidades_total"], ascending=False).reset_index(drop=True)

g.to_parquet(os.path.join(BASE, "data/competencia", "comparativa_venta.parquet"), index=False)
cols_visibles = ["modelo_syscom", "descripcion_syscom", "marca", "unidades_8s",
                 "unidades_total", "n_distribuidores", "precio_lista_sys",
                 "subtotal_sys", "pv_comp_med", "pv_comp_min", "vs_lista_med",
                 "vs_sub_med"]
g[cols_visibles].to_csv(os.path.join(BASE, "out", "comparativa_venta.csv"), index=False)

print(f"listado: {len(g):,} SYSCOM únicos ordenados por venta")
print(g[["modelo_syscom", "unidades_8s", "unidades_total", "n_distribuidores",
         "precio_lista_sys", "pv_comp_med", "vs_lista_med"]].head(12).to_string())