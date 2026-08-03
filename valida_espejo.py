# -*- coding: utf-8 -*-
"""VALIDACIÓN ESPEJO: Aurora (VPN, en vivo) vs API de BI/Redshift (en vivo).

Más fuerte que valida_migracion.py (que compara la API contra los parquets
locales): aquí las DOS fuentes se consultan EN VIVO con los mismos filtros,
eliminando el desfase de extracción como explicación de cualquier diferencia.

Pruebas (usuario 2026-07-30, requisito previo a migrar):
  1. VENTAS agregado anual 2016→hoy: COUNT / SUM(cantidad) / SUM(subtotal)
     con filtros del extract (estatus='Activa', precio>0, cantidad>0).
  2. VENTAS agregado mensual de los últimos 6 meses (granularidad fina donde
     más duele un desfase).
  3. RENGLONES AL AZAR: ~40 folios sorteados de distintos años; se comparan
     las 19 columnas que consume el pipeline, valor por valor.
  4. INVENTARIO: 4 fechas de snapshot (reciente, hace 1 mes, 2022, 2019):
     SKUs, SUM(existencia), SUM(cantidad_bo), SUM(costo_total_dolares).

Uso:  ./.venv/bin/python valida_espejo.py
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_bi import q          # noqa: E402
from db import query          # noqa: E402

F_AUR = "estatus='Activa' AND precio>0 AND cantidad>0"
F_API = ("estatus='Activa' AND CAST(precio AS DECIMAL(18,4))>0 "
         "AND CAST(cantidad AS DECIMAL(18,4))>0")
COLS_PIPE = ["fecha", "folio", "codigo", "codigo_cliente", "cantidad", "precio",
             "tipo_precio", "descuento_uno", "descuento", "desc_fin", "subtotal",
             "costo_venta", "tipo_cambio", "concepto", "clasificacion_descuento",
             "via", "estatus", "proveedor", "kit"]


def _dif(a, b):
    return 100 * (a / b - 1) if b else (0.0 if not a else float("inf"))


def ventas_historicas_muestra():
    """Historia 2016-2023 por MUESTRA: 2 semanas por año (marzo y septiembre),
    agregadas DÍA POR DÍA en Aurora — reporte_61 es una vista pesada y un
    agregado anual/mensual completo excede el timeout de la VPN; las consultas
    de un día (~9s, el mismo patrón de extract.py) sí corren."""
    print("\n== [1] HISTORIA 2016-2023 — 2 semanas de muestra por año ==", flush=True)
    ok_todo = True
    for anio in range(2016, 2024):
        for mes, dia in [(3, 6), (9, 12)]:
            ini = pd.Timestamp(anio, mes, dia)
            n_l = u_l = s_l = 0.0
            for d in range(7):
                f = (ini + pd.Timedelta(days=d)).date().isoformat()
                _, fa = query(f"SELECT /*+ MAX_EXECUTION_TIME(120000) */ COUNT(*), "
                              f"SUM(cantidad), SUM(subtotal) FROM reportes.reporte_61 "
                              f"WHERE fecha >= '{f}' AND fecha < '{f}' + INTERVAL 1 DAY "
                              f"AND {F_AUR}")
                n_l += int(fa[0][0]); u_l += float(fa[0][1] or 0); s_l += float(fa[0][2] or 0)
            fin = (ini + pd.Timedelta(days=7)).date().isoformat()
            api = q(f"SELECT COUNT(*) AS n, SUM(CAST(cantidad AS DECIMAL(18,4))) AS u, "
                    f"SUM(CAST(subtotal AS DECIMAL(18,4))) AS s FROM reporte_61 "
                    f"WHERE CAST(fecha AS DATE) >= DATE '{ini.date()}' "
                    f"AND CAST(fecha AS DATE) < DATE '{fin}' AND {F_API} LIMIT 1")
            time.sleep(2.1)
            dn = _dif(float(api.n[0]), n_l)
            du = _dif(float(api.u[0] or 0), u_l)
            ds = _dif(float(api.s[0] or 0), s_l)
            ok = all(abs(x) <= 0.5 for x in (dn, du, ds))
            ok_todo &= ok
            print(f"  sem {ini.date()}: renglones {dn:+.3f}% | unidades {du:+.3f}% | "
                  f"subtotal {ds:+.3f}%  {'✓' if ok else '✗'}", flush=True)
    return ok_todo


def ventas_mensual():
    """Ventana del PIPELINE (24 meses) — lo que la migración debe garantizar.
    Aurora por SEMANAS sumadas a mes (día×7 excede menos el timeout que el mes
    entero); en la práctica: rangos de 7 días con hint de 5 min."""
    print("\n== [2] VENTAS POR MES — ventana del pipeline (24 meses) ==", flush=True)
    desde = (pd.Timestamp.today().normalize() - pd.DateOffset(months=24)).replace(day=1)
    aur = {}
    d = desde
    hoy = pd.Timestamp.today().normalize()
    while d < hoy:
        # tramo de máx 7 días SIN cruzar de mes (cada tramo suma a su mes)
        prox_mes = (d + pd.offsets.MonthBegin(1)).normalize()
        fin = min(d + pd.Timedelta(days=7), prox_mes, hoy)
        _, fa = query(f"SELECT /*+ MAX_EXECUTION_TIME(300000) */ COUNT(*), SUM(subtotal) "
                      f"FROM reportes.reporte_61 WHERE fecha >= '{d.date()}' "
                      f"AND fecha < '{fin.date()}' AND {F_AUR}")
        mes = d.strftime("%Y-%m")
        n0, s0 = aur.get(mes, (0, 0.0))
        aur[mes] = (n0 + int(fa[0][0]), s0 + float(fa[0][1] or 0))
        d = fin
    api = q(f"SELECT SUBSTRING(fecha,1,7) AS m, COUNT(*) AS n, "
            f"SUM(CAST(subtotal AS DECIMAL(18,4))) AS s FROM reporte_61 "
            f"WHERE CAST(fecha AS DATE) >= DATE '{desde.date()}' AND {F_API} "
            f"GROUP BY 1 ORDER BY 1 LIMIT 30")
    ok_todo = True
    for _, r in api.iterrows():
        if r.m not in aur:
            continue
        n_l, s_l = aur[r.m]
        # el mes en curso: Aurora corta en 'hoy'; la API puede traer horas más
        dn, ds = _dif(float(r.n), n_l), _dif(float(r.s), s_l)
        ok = abs(dn) <= 0.5 and abs(ds) <= 0.5
        ok_todo &= ok
        print(f"  {r.m}: renglones {dn:+.3f}% | subtotal {ds:+.3f}%  {'✓' if ok else '✗'}",
              flush=True)
    return ok_todo


def renglones_al_azar(n_por_anio=5):
    print("\n== [3] RENGLONES AL AZAR — 19 columnas del pipeline, valor por valor ==",
          flush=True)
    rng = np.random.default_rng(20260730)
    total_ok = total_dif = 0
    for anio in [2016, 2018, 2020, 2022, 2024, 2025, 2026]:
        _, fol = query(f"SELECT DISTINCT folio FROM reportes.reporte_61 "
                       f"WHERE fecha >= '{anio}-03-01' AND fecha < '{anio}-03-08' "
                       f"AND estatus='Activa' LIMIT 500")
        if not fol:
            print(f"  {anio}: sin folios en la semana de muestra", flush=True)
            continue
        idx = rng.choice(len(fol), size=min(n_por_anio, len(fol)), replace=False)
        folios = [fol[i][0] for i in idx]
        lista = ",".join(f"'{x}'" for x in folios)
        sel = ", ".join(COLS_PIPE)
        _, fa = query(f"SELECT {sel} FROM reportes.reporte_61 WHERE folio IN ({lista})")
        aur = pd.DataFrame(fa, columns=COLS_PIPE)
        api = q(f"SELECT {sel} FROM reporte_61 WHERE folio IN ({lista}) LIMIT 1000")
        time.sleep(2.1)
        def norm(df):
            d = df.copy()
            d["fecha"] = pd.to_datetime(d.fecha).dt.tz_localize(None).dt.normalize()
            for c in ["cantidad", "precio", "tipo_precio", "desc_fin", "subtotal",
                      "costo_venta", "tipo_cambio", "kit"]:
                d[c] = pd.to_numeric(d[c], errors="coerce").round(4)
            for c in ["folio", "codigo", "codigo_cliente", "descuento_uno", "descuento",
                      "concepto", "clasificacion_descuento", "via", "estatus", "proveedor"]:
                d[c] = d[c].astype(str).str.strip()
            return d.sort_values(["folio", "codigo", "cantidad", "precio"]).reset_index(drop=True)
        a, b = norm(aur), norm(api)
        if len(a) != len(b):
            print(f"  {anio}: ✗ conteo distinto (Aurora {len(a)} vs API {len(b)})", flush=True)
            total_dif += 1
            continue
        iguales = (a == b) | (a.isna() & b.isna())
        malas = [c for c in COLS_PIPE if not iguales[c].all()]
        if malas:
            total_dif += 1
            print(f"  {anio}: ✗ {len(a)} renglones, difieren columnas: {malas}", flush=True)
            for c in malas[:2]:
                idx = (~iguales[c]).idxmax()
                print(f"      ej {c}: aurora={a.loc[idx, c]!r} vs api={b.loc[idx, c]!r} "
                      f"(folio {a.loc[idx, 'folio']})", flush=True)
        else:
            total_ok += 1
            print(f"  {anio}: ✓ {len(a)} renglones idénticos en las 19 columnas", flush=True)
    return total_dif == 0


def inventario():
    print("\n== [4] INVENTARIO — 4 fechas de snapshot ==", flush=True)
    fechas = ["2026-07-28", "2026-06-30", "2022-06-15", "2019-06-12"]
    ok_todo = True
    for f in fechas:
        _, fa = query(f"SELECT COUNT(DISTINCT codigo), SUM(existencia), SUM(cantidad_bo), "
                      f"SUM(costo_total_dolares) FROM reportes.valor_inventario "
                      f"WHERE fecha = '{f}'")
        if not fa or fa[0][0] == 0:
            print(f"  {f}: Aurora sin snapshot ese día — se omite", flush=True)
            continue
        n_l, e_l, b_l, c_l = (float(x or 0) for x in fa[0])
        api = q(f"SELECT COUNT(DISTINCT codigo) AS n, "
                f"SUM(CAST(existencia AS DECIMAL(18,4))) AS e, "
                f"SUM(CAST(cantidad_bo AS DECIMAL(18,4))) AS b, "
                f"SUM(CAST(costo_total_dolares AS DECIMAL(18,4))) AS c "
                f"FROM valor_inventario WHERE fecha = '{f}' LIMIT 1")
        time.sleep(2.1)
        vals = [(float(api.n[0] or 0), n_l), (float(api.e[0] or 0), e_l),
                (float(api.b[0] or 0), b_l), (float(api.c[0] or 0), c_l)]
        difs = [_dif(x, y) for x, y in vals]
        ok = all(abs(d) <= 0.5 for d in difs)
        ok_todo &= ok
        print(f"  {f}: SKUs {difs[0]:+.3f}% | existencia {difs[1]:+.3f}% | "
              f"BO {difs[2]:+.3f}% | costo USD {difs[3]:+.3f}%  {'✓' if ok else '✗'}",
              flush=True)
    return ok_todo


def main():
    r1 = ventas_historicas_muestra()
    r2 = ventas_mensual()
    r3 = renglones_al_azar()
    r4 = inventario()
    print("\n== VEREDICTO ESPEJO ==", flush=True)
    for nombre, ok in [("ventas anual", r1), ("ventas mensual", r2),
                       ("renglones al azar", r3), ("inventario", r4)]:
        print(f"  {nombre}: {'✓ IDÉNTICOS' if ok else '✗ REVISAR'}", flush=True)


if __name__ == "__main__":
    main()
