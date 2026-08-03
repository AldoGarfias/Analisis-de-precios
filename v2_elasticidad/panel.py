# -*- coding: utf-8 -*-
"""Construye el panel balanceado SKU × semana para estimar elasticidad.

Clave del diseño: la variable de precio es el **precio de lista** `p1` (la palanca
que mueve el panel), conocido en TODAS las semanas (serie escalonada de
`historial_precios_asteriscos`). Así las semanas con venta 0 entran como demanda 0
a ese precio (informativas) y el estimador de conteo (PPML) las usa sin el problema
de log(0). El precio realizado sólo se usa para el margen y el pass-through.

Instrumentos exógenos: costo USD del proveedor (`cos_prom_dlls`) y FX macro semanal
(media de `tipo_cambio`). Ambos mueven `p1` vía el factor, no la demanda del SKU.

Salida: data/elast/panel.parquet  y  data/elast/baseline.parquet (por SKU).
"""
import os

import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "elast")


def _lunes(s):
    """Lunes de la semana ISO de cada fecha (semana anclada en lunes)."""
    s = pd.to_datetime(s)
    return (s - pd.to_timedelta(s.dt.weekday, unit="D")).dt.normalize()


def construir():
    art = pd.read_parquet(f"{DATA}/articulos.parquet")
    ventas = pd.read_parquet(f"{DATA}/ventas.parquet")
    hist = pd.read_parquet(f"{DATA}/precios_hist.parquet")
    costos = pd.read_parquet(f"{DATA}/costos.parquet")
    clusters = pd.read_parquet(f"{DATA}/clusters.parquet")

    for df in (ventas, hist, costos):
        df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    ventas = ventas.dropna(subset=["fecha"])

    # --- agregar venta a SKU×semana (canales mezclados; heterogeneidad de canal = v3)
    ventas["week"] = _lunes(ventas["fecha"])
    ventas["unidades"] = ventas["cantidad"].astype(float) - ventas["cantidad_nc"].astype(float)
    ventas["rev_usd"] = ventas["precio_usd"].astype(float) * ventas["cantidad"].astype(float)
    agg = ventas.groupby(["id_art", "week"]).agg(
        unidades=("unidades", "sum"),
        rev_usd=("rev_usd", "sum"),
        piezas=("cantidad", "sum"),
        fx=("tipo_cambio", "mean"),
    ).reset_index()
    agg["unidades"] = agg["unidades"].clip(lower=0)  # NC > ventas en semana rara -> 0
    agg["precio_real"] = np.where(agg["piezas"] > 0, agg["rev_usd"] / agg["piezas"], np.nan)

    # FX macro semanal (choque común, exógeno) para instrumentar y rellenar
    fx_week = ventas.groupby("week")["tipo_cambio"].mean().rename("fx_macro")

    # --- rejilla balanceada: todos los SKU × todas las semanas del rango
    semanas = pd.date_range(agg["week"].min(), agg["week"].max(), freq="W-MON")
    skus = art["id_art"].astype(int).unique()
    grid = pd.MultiIndex.from_product([skus, semanas], names=["id_art", "week"]).to_frame(index=False)

    panel = grid.merge(agg, on=["id_art", "week"], how="left")
    panel["unidades"] = panel["unidades"].fillna(0.0)
    panel = panel.merge(fx_week, on="week", how="left")

    # --- precio de lista p1 (asof backward) + relleno hacia atrás del inicio
    panel = _asof_por_sku(panel, hist[["id_art", "fecha", "p1"]], "p1")
    panel = _asof_por_sku(panel, hist[["id_art", "fecha", "p3"]], "p3")

    # --- costo USD (asof). costos_prom es por (id_art,id_ast,fecha) -> promedio a id_art×fecha
    cost = (costos.groupby(["id_art", "fecha"])["cos_prom_dlls"].mean().reset_index())
    panel = _asof_por_sku(panel, cost, "cos_prom_dlls")
    panel = panel.rename(columns={"cos_prom_dlls": "costo"})

    # --- precio del sustituto más cercano (su p1 esa semana) para cross-price
    p1_por_sku_sem = panel[["id_art", "week", "p1"]].rename(
        columns={"id_art": "id_sust_cercano", "p1": "precio_sust"})
    panel = panel.merge(clusters, on="id_art", how="left")
    panel = panel.merge(p1_por_sku_sem, on=["id_sust_cercano", "week"], how="left")

    panel = panel.merge(art[["id_art", "marca", "linea", "codigo", "moneda",
                             "precio_1", "precio_3", "es_kit"]], on="id_art", how="left")

    # tipos: MySQL entrega double/Decimal como object -> forzar float
    for c in ["p1", "p3", "costo", "fx", "fx_macro", "precio_sust",
              "precio_real", "unidades", "piezas", "precio_1", "precio_3"]:
        if c in panel.columns:
            panel[c] = pd.to_numeric(panel[c], errors="coerce")

    # recortar semanas ANTERIORES a la primera venta de cada SKU: la rejilla
    # balanceada + el bfill de p1/costo fabricarían semanas de demanda-0 previas a
    # que el SKU existiera, sesgando la elasticidad. Los SKUs que nunca vendieron
    # (sin primera venta) se descartan: no informan elasticidad ni son accionables.
    primera = agg.loc[agg["piezas"].fillna(0) > 0].groupby("id_art")["week"].min()
    panel["_primera"] = panel["id_art"].map(primera)
    panel = panel[panel["week"] >= panel["_primera"]].drop(columns="_primera")

    # limpieza: necesitamos p1 y costo válidos para identificar
    panel = panel.dropna(subset=["p1", "costo"])
    panel = panel[(panel["p1"] > 0) & (panel["costo"] > 0)]
    panel["fx"] = panel["fx"].fillna(panel["fx_macro"])
    panel["mes"] = panel["week"].values.astype("datetime64[M]")
    panel["log_unidades"] = np.log1p(panel["unidades"])
    panel["log_p1"] = np.log(panel["p1"])
    panel["log_costo"] = np.log(panel["costo"])
    panel["log_psust"] = np.log(panel["precio_sust"].where(panel["precio_sust"] > 0))

    panel.to_parquet(f"{DATA}/panel.parquet", index=False)
    print(f"panel.parquet: {len(panel)} filas SKU×semana, {panel['id_art'].nunique()} SKUs, "
          f"{panel['week'].nunique()} semanas", flush=True)

    _baseline(panel, hist)
    return panel


def _asof_por_sku(panel, serie, col):
    """merge_asof backward de `serie[col]` por SKU sobre week; rellena inicio con bfill."""
    serie = serie.dropna(subset=["fecha"]).sort_values("fecha")
    p = panel.sort_values("week")
    out = pd.merge_asof(p, serie, left_on="week", right_on="fecha", by="id_art",
                        direction="backward")
    out = out.drop(columns=[c for c in ("fecha",) if c in out.columns])
    # semanas anteriores al primer dato -> tomar el primer valor conocido del SKU
    out[col] = out.groupby("id_art")[col].transform(lambda s: s.bfill())
    return out


def _baseline(panel, hist):
    """Métricas por SKU para el optimizador y la validación."""
    vendido = panel[panel["piezas"].fillna(0) > 0]
    base = panel.groupby("id_art").agg(
        marca=("marca", "first"), linea=("linea", "first"), codigo=("codigo", "first"),
        moneda=("moneda", "first"), es_kit=("es_kit", "first"),
        cluster_id=("cluster_id", "first"),
        precio_1=("precio_1", "first"), precio_3=("precio_3", "first"),
        costo=("costo", "last"), p1_actual=("p1", "last"), p3_actual=("p3", "last"),
        semanas=("week", "nunique"),
        unidades_prom_sem=("unidades", "mean"),
        unidades_tot=("unidades", "sum"),
    ).reset_index()
    real = vendido.groupby("id_art").agg(
        precio_real_med=("precio_real", "median"),
        semanas_venta=("week", "nunique"),
    ).reset_index()
    # nº de cambios de precio (variación de p1 en el histórico) para peso EB
    ncamb = (hist.groupby("id_art")["p1"].nunique().rename("n_cambios_p1").reset_index())
    base = base.merge(real, on="id_art", how="left").merge(ncamb, on="id_art", how="left")
    base["semanas_venta"] = base["semanas_venta"].fillna(0).astype(int)
    base["n_cambios_p1"] = base["n_cambios_p1"].fillna(1).astype(int)
    # ratio realizado/lista (pass-through de nivel) y margen actual
    base["rho"] = (base["precio_real_med"] / base["p1_actual"]).clip(0.3, 1.2)
    base["rho"] = base["rho"].fillna(base["rho"].median())
    base.to_parquet(f"{DATA}/baseline.parquet", index=False)
    print(f"baseline.parquet: {len(base)} SKUs "
          f"({base['semanas_venta'].gt(0).sum()} con venta)", flush=True)


if __name__ == "__main__":
    construir()
