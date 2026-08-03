# -*- coding: utf-8 -*-
"""Extrae a parquet los datos del motor de elasticidad (venta real).

Solo SELECTs contra la réplica de lectura (reusa scripts/precio_optimo/db.py).
Fuentes verificadas en BD (2026-07-22):
  - reportes.art_vnts_por_mes : venta transaccional (rolling 12m), precio realizado,
    FX real por transacción, canal (concepto), cliente.
  - historial_precios_asteriscos : serie del precio de lista (la palanca), con fecha.
  - costos_prom : costo USD/pesos por fecha (usar `fecha`, no `anio` que viene 0).
  - cat_sustitutos : sustitutos curados (para clusters de similares).

Se filtra por marca (join a cat_articulos2) para acotar el volumen al piloto.
Salida en data/elast/.
"""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import conectar_erp, query  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "elast")
FECHA_INI = "2025-07-20"  # inicio del rolling 12m de art_vnts_por_mes

# Marcas piloto (densas en cambios de precio + importadas -> instrumento FX limpio).
MARCAS_DEFAULT = ["HIKVISION", "UBIQUITI", "LINKEDPRO BY EPCOM", "PANDUIT"]

ART = "`cat_art&iacute;culos2`"
ID = "`id_art&iacute;culo`"
COD = "`c&oacute;digo_art&iacute;culo`"


def _in_marcas(marcas):
    return ",".join("'" + m.replace("'", "''") + "'" for m in marcas)


def extraer_articulos(con, marcas):
    sql = f"""
        SELECT {ID} AS id_art, {COD} AS codigo, linea, marca, moneda, estatus,
               precio_1, precio_3, factor_precio_1, factor_precio_3,
               costo_prov_dlls, remate, kit AS es_kit
        FROM {ART}
        WHERE marca IN ({_in_marcas(marcas)})
    """
    cols, filas = query(sql, con=con)
    df = pd.DataFrame(filas, columns=cols)
    df.to_parquet(f"{DATA}/articulos.parquet", index=False)
    print(f"articulos.parquet: {len(df)} SKUs", flush=True)
    return df


def extraer_ventas(con, marcas):
    """Venta transaccional por marca (fac_cancelada=0). Loop por marca para progreso."""
    partes = []
    t0 = time.time()
    for m in marcas:
        sql = f"""
            SELECT v.id_art, v.fecha, v.cantidad, v.cantidad_nc,
                   v.precio_usd, v.precio_mxn, v.tipo_cambio,
                   v.concepto, v.id_cliente, v.tipo_doc
            FROM reportes.art_vnts_por_mes v
            JOIN {ART} a ON a.{ID} = v.id_art
            WHERE v.fac_cancelada = 0 AND v.fecha >= %s AND a.marca = %s
        """
        cols, filas = query(sql, (FECHA_INI, m), con=con)
        if filas:
            partes.append(pd.DataFrame(filas, columns=cols))
            ultimas_cols = cols
        print(f"  ventas {m}: +{len(filas)} ({time.time()-t0:.0f}s)", flush=True)
    if not partes:
        raise SystemExit(f"Sin ventas para {marcas} desde {FECHA_INI}: revisa el "
                         "nombre exacto de la marca en cat_articulos2.")
    df = pd.concat(partes, ignore_index=True)
    df.to_parquet(f"{DATA}/ventas.parquet", index=False)
    print(f"ventas.parquet: {len(df)} renglones", flush=True)


def extraer_precios_hist(con, marcas):
    """Serie del precio de lista (p1) por SKU, con fecha del cambio."""
    sql = f"""
        SELECT h.id_modelo AS id_art, h.fecha, h.p1, h.p3
        FROM historial_precios_asteriscos h
        JOIN {ART} a ON a.{ID} = h.id_modelo
        WHERE a.marca IN ({_in_marcas(marcas)}) AND h.fecha >= '2024-01-01'
              AND h.p1 > 0
    """
    cols, filas = query(sql, con=con)
    df = pd.DataFrame(filas, columns=cols)
    df.to_parquet(f"{DATA}/precios_hist.parquet", index=False)
    print(f"precios_hist.parquet: {len(df)} cambios", flush=True)


def extraer_costos(con, marcas):
    sql = f"""
        SELECT c.id_art, c.id_ast, c.fecha, c.cos_prom_dlls, c.cos_prom_pesos
        FROM costos_prom c
        JOIN {ART} a ON a.{ID} = c.id_art
        WHERE a.marca IN ({_in_marcas(marcas)}) AND c.fecha >= '2024-06-01'
              AND c.cos_prom_dlls > 0
    """
    cols, filas = query(sql, con=con)
    df = pd.DataFrame(filas, columns=cols)
    df.to_parquet(f"{DATA}/costos.parquet", index=False)
    print(f"costos.parquet: {len(df)} filas", flush=True)


def extraer_sustitutos(con):
    """cat_sustitutos completo (chico) para armar clusters de similares."""
    cols, filas = query(
        "SELECT `id_art&iacute;culo` AS id_art, "
        "`id_art&iacute;culo_sustituto` AS id_sust, fecha FROM cat_sustitutos", con=con)
    df = pd.DataFrame(filas, columns=cols)
    df.to_parquet(f"{DATA}/sustitutos.parquet", index=False)
    print(f"sustitutos.parquet: {len(df)} links", flush=True)


def extraer_todo(con, marcas):
    """Corre las 5 extracciones (usado por __main__ y por run.py)."""
    os.makedirs(DATA, exist_ok=True)
    extraer_articulos(con, marcas)
    extraer_precios_hist(con, marcas)
    extraer_costos(con, marcas)
    extraer_sustitutos(con)
    extraer_ventas(con, marcas)


if __name__ == "__main__":
    marcas = sys.argv[1].split(",") if len(sys.argv) > 1 else MARCAS_DEFAULT
    print("marcas:", marcas, flush=True)
    con = conectar_erp()
    try:
        extraer_todo(con, marcas)
    finally:
        con.close()
    print("EXTRACCION VENTAS COMPLETA", flush=True)
