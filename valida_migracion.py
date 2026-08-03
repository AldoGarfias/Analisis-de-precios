# -*- coding: utf-8 -*-
"""VALIDADOR DE MIGRACIÓN Aurora → API de BI (Redshift).

Plan acordado (usuario 2026-07-29): migrar base por base CONFIRMANDO que los
datos que necesitamos están, ANTES de mover cualquier consumidor del pipeline.

Para cada tabla objetivo (reporte_61, valor_inventario), cuando aparezca en la
allow-list:
  1. CENSO DE COLUMNAS: qué trae la API vs qué necesita el pipeline.
     Faltantes = BLOQUEADORES (se piden a IT antes de migrar).
  2. RECONCILIACIÓN DE VENTAS: por cada uno de los últimos 3 meses cerrados,
     COUNT/SUM(cantidad)/SUM(subtotal) de la API (con los mismos filtros del
     pipeline, si las columnas existen) vs nuestro parquet local (extraído de
     Aurora). Deben cuadrar ±0.5%.
  3. RECONCILIACIÓN DE INVENTARIO: para una fecha de snapshot que tengamos
     local, totales por codigo (muestra top-20) y agregado.

OJO Redshift (doc del ejemplo): TODAS las columnas son VARCHAR → castear
(CAST(x AS DECIMAL(18,4)), CAST(fecha AS DATE)); no mezclar MySQL/Redshift.

Uso:  ./.venv/bin/python valida_migracion.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_bi import q, tablas   # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))

# columnas que el pipeline CONSUME hoy (extract.py) — el estándar del censo
REQ_R61 = ["fecha", "folio", "codigo", "codigo_cliente", "cantidad", "precio",
           "tipo_precio", "descuento_uno", "descuento", "desc_fin", "subtotal",
           "costo_venta", "tipo_cambio", "concepto", "clasificacion_descuento",
           "via", "estatus", "proveedor", "kit"]
REQ_VI = ["fecha", "codigo", "almacen", "existencia", "existencia_total",
          "cantidad_bo", "precio_1", "precio_3", "costo_prov",
          "costo_total_dolares"]


def _cols_api(tabla):
    df = q(f"SELECT * FROM {tabla} LIMIT 1")
    return list(df.columns)


def _censo(tabla, req):
    print(f"\n== CENSO DE COLUMNAS: {tabla} ==", flush=True)
    cols = _cols_api(tabla)
    print(f"  la API trae {len(cols)}: {cols}", flush=True)
    faltan = [c for c in req if c not in cols]
    if faltan:
        print(f"  ⛔ BLOQUEADORES (el pipeline las necesita y NO están): {faltan}",
              flush=True)
        print(f"     → pedir a IT que las agregue a la vista antes de migrar", flush=True)
    else:
        print(f"  ✓ todas las columnas requeridas están", flush=True)
    return cols, faltan


def _reconcilia_ventas(cols):
    print(f"\n== RECONCILIACIÓN DE VENTAS (API vs parquet local de Aurora) ==", flush=True)
    import glob
    rutas = sorted(glob.glob(os.path.join(BASE, "data", "reporte61", "ventas_*.parquet")))[-4:-1]
    # filtros EXACTOS de extract.py (estatus/precio/cantidad y NADA MÁS).
    # OJO (lección 2026-07-30): NO agregar tipo_precio IN (1,3) — ese filtro es
    # del panel, no del extract; agregarlo aquí creó un falso déficit de −9.2%
    # en junio (del 18 al 29 de junio ~la mitad de la venta se registró con
    # tipo_precio=0 EN AMBAS BASES — evento real de negocio, no hueco de datos).
    filtros = []
    if "estatus" in cols:
        filtros.append("estatus = 'Activa'")
    if "precio" in cols:
        filtros.append("CAST(precio AS DECIMAL(18,4)) > 0")
    if "cantidad" in cols:
        filtros.append("CAST(cantidad AS DECIMAL(18,4)) > 0")
    w_extra = (" AND " + " AND ".join(filtros)) if filtros else ""
    comparable = {"estatus", "precio", "cantidad"}.issubset(cols)
    if not comparable:
        print("  ⚠ faltan columnas de filtro: la comparación será APROXIMADA "
              "(la API sin filtrar vs nuestro parquet filtrado)", flush=True)
    for ruta in rutas:
        mes = os.path.basename(ruta)[7:14].replace("_", "-")
        loc = pd.read_parquet(ruta, columns=["cantidad", "subtotal"])
        api = q(f"SELECT COUNT(*) AS n, SUM(CAST(cantidad AS DECIMAL(18,4))) AS u, "
                f"SUM(CAST(subtotal AS DECIMAL(18,4))) AS s FROM reporte_61 "
                f"WHERE CAST(fecha AS DATE) >= DATE '{mes}-01' "
                f"AND CAST(fecha AS DATE) < DATE '{mes}-01' + INTERVAL '1 month'"
                f"{w_extra} LIMIT 10")
        n_a, u_a, s_a = float(api.n[0]), float(api.u[0] or 0), float(api.s[0] or 0)
        n_l, u_l, s_l = len(loc), float(loc.cantidad.astype(float).sum()), float(loc.subtotal.astype(float).sum())
        dif = lambda a, b: 100 * (a / b - 1) if b else float("inf")
        ok = all(abs(dif(x, y)) <= 0.5 for x, y in [(n_a, n_l), (u_a, u_l), (s_a, s_l)])
        print(f"  {mes}: renglones API {n_a:,.0f} vs local {n_l:,} ({dif(n_a,n_l):+.2f}%) | "
              f"unidades {dif(u_a,u_l):+.2f}% | subtotal {dif(s_a,s_l):+.2f}%  "
              f"{'✓' if ok else '✗ REVISAR'}", flush=True)


def _reconcilia_inventario(cols):
    print(f"\n== RECONCILIACIÓN DE INVENTARIO (API vs snapshot local) ==", flush=True)
    ex = pd.read_parquet(os.path.join(BASE, "data", "reporte61", "existencias_sem.parquet"))
    fecha = pd.Timestamp(ex.semana.max()).date().isoformat()
    loc = ex[ex.semana == ex.semana.max()]
    api = q(f"SELECT COUNT(DISTINCT codigo) AS skus, "
            f"SUM(CAST(existencia AS DECIMAL(18,4))) AS exi "
            f"FROM valor_inventario WHERE fecha = '{fecha}' LIMIT 10")
    if len(api) == 0 or pd.isna(api.skus[0]):
        print(f"  la API no tiene el snapshot {fecha} — probar otra fecha", flush=True)
        return
    skus_a, exi_a = float(api.skus[0]), float(api.exi[0] or 0)
    skus_l, exi_l = loc.codigo.nunique(), float(loc.disponible.sum())
    print(f"  snapshot {fecha}: SKUs API {skus_a:,.0f} vs local {skus_l:,} | "
          f"existencia API {exi_a:,.0f} vs local (sin apartada) {exi_l:,.0f}", flush=True)
    print(f"  (diferencias esperables: la API suma TODOS los almacenes y CON "
          f"apartada; el local ya viene agregado — validar por muestra de códigos)", flush=True)
    top = loc.nlargest(10, "disponible")[["codigo", "disponible"]]
    lista = ",".join(f"'{c}'" for c in top.codigo)
    api2 = q(f"SELECT codigo, SUM(CAST(existencia AS DECIMAL(18,4))) AS exi "
             f"FROM valor_inventario WHERE fecha = '{fecha}' AND codigo IN ({lista}) "
             f"GROUP BY codigo LIMIT 20")
    m = top.merge(api2, on="codigo", how="left")
    m["dif_pct"] = 100 * (m.exi.astype(float) / m.disponible - 1)
    print(m.to_string(index=False), flush=True)


def main():
    nombres = [t["name"] for t in tablas()["tables"]]
    hechas = 0
    if "reporte_61" in nombres:
        cols, faltan = _censo("reporte_61", REQ_R61)
        _reconcilia_ventas(cols)
        hechas += 1
    else:
        print("\nreporte_61: AÚN NO habilitada en la allow-list", flush=True)
    if "valor_inventario" in nombres:
        cols_vi, faltan_vi = _censo("valor_inventario", REQ_VI)
        _reconcilia_inventario(cols_vi)
        hechas += 1
    else:
        print("valor_inventario: AÚN NO habilitada en la allow-list", flush=True)
    if hechas == 0:
        print("\n(el vigilante diario avisará en cuanto aparezcan; entonces "
              "correr este validador)", flush=True)


if __name__ == "__main__":
    main()
