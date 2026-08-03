# -*- coding: utf-8 -*-
"""Recomendaciones de precio por SKU para el próximo mes.

Toma los renglones cotizados de los últimos 90 días, barre el precio relativo
(rel_precio) sobre el modelo win-rate — vectorizado: una predicción global por
punto del grid — y por SKU elige el precio que maximiza utilidad esperada:
    argmax_r  P(win | r, renglón) × (r − costo/lista) × lista × cantidad
Guardrails:
  - movimiento máximo ±10 pts de rel_precio vs el nivel actual del SKU
  - piso de margen: rel_precio >= costo/lista + 3 pts
  - mínimo 20 cotizaciones ÚNICAS (ya deduplicadas por cliente-SKU-semana
    en model.py) en 90 días para opinar
Correcciones incorporadas (en línea con model.py):
  - rel_precio y costo van contra la LISTA APLICABLE (precio 1 o 3 según
    tipo_precio); el monto y la utilidad se calculan sobre esa misma lista
  - la utilidad esperada y el win-rate observado se PONDERAN por `peso`
    (interés real estimado de cada cotización)
Salida: out/recomendaciones.csv (rankeada por impacto/mes) y out/resumen_recos.txt

Uso: .venv/bin/python recommend.py
"""
import sys

import duckdb
import joblib
import numpy as np
import pandas as pd

from model import preparar_matriz

VENTANA_DIAS = 90
# Tras el dedup semanal el conteo por SKU baja: 20 cotizaciones únicas es más
# exigente que 20 renglones crudos. Si quedan muy pocos SKUs, considerar 15.
MIN_LINEAS = 20
MAX_LINEAS_SKU = 300
GRID = np.round(np.arange(0.55, 1.041, 0.01), 2)
MAX_MOV = 0.10
PISO_MARGEN = 0.03
UMBRAL_ACCION = 0.02

COLS = """fecha_alta, id_art, id_asterisco, won, cantidad, precio, precio_lista,
          lista_aplicable, peso, costo_sobre_lista, rel_precio, rel_vs_tipico,
          log_cantidad, log_monto, log_total_art, mes, es_precio_3, linea,
          marca, id_proveedor, clasif_cliente, id_sucursal"""


def cargar_ventana():
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT {COLS}
        FROM 'out/dataset_modelo.parquet'
        WHERE fecha_alta >= (SELECT MAX(fecha_alta) - INTERVAL {VENTANA_DIAS} DAY
                             FROM 'out/dataset_modelo.parquet')
    """).df()
    con.close()
    return df


def main():
    pack = joblib.load("out/modelo.joblib")
    modelo, columnas, mapas = pack["modelo"], pack["columnas"], pack["mapas"]

    rec = cargar_ventana()
    print(f"ventana {VENTANA_DIAS}d: {len(rec)} renglones", flush=True)

    # SKUs con actividad suficiente; muestrear renglones por SKU para acotar cómputo
    rec["sku"] = rec["id_art"].astype(str) + "*" + rec["id_asterisco"].astype(str)
    tam = rec.groupby("sku")["sku"].transform("size")
    rec = rec[tam >= MIN_LINEAS]
    rec = rec.groupby("sku", group_keys=False).apply(
        lambda g: g.sample(min(len(g), MAX_LINEAS_SKU), random_state=0))
    rec = rec.reset_index(drop=True)
    print(f"SKUs candidatos: {rec['sku'].nunique()} ({len(rec)} renglones)", flush=True)

    X = preparar_matriz(rec, mapas)[columnas].astype(np.float32)
    med = (X["rel_precio"] - X["rel_vs_tipico"]).values

    # costo/lista por SKU (mediana); sin costo confiable no se recomienda
    c_sku = rec.groupby("sku")["costo_sobre_lista"].median()
    c_fila = rec["sku"].map(c_sku).values
    # monto sobre la LISTA APLICABLE (misma base que rel_precio y costo)
    monto = (rec["lista_aplicable"].astype(float) * rec["cantidad"].astype(float)).values
    peso = rec["peso"].astype(float).values

    # matrices (grid × renglón): P(win) y utilidad
    n = len(rec)
    P = np.empty((len(GRID), n), dtype=np.float32)
    for i, r in enumerate(GRID):
        Xi = X.copy()
        Xi["rel_precio"] = r
        Xi["rel_vs_tipico"] = r - med
        P[i] = modelo.predict_proba(Xi)[:, 1]
        if i % 10 == 0:
            print(f"  grid {r:.2f}", flush=True)
    # utilidad esperada ponderada por peso: peso ≈ prob. de que la cotización
    # tenga interés real, así la utilidad agregada no la inflan renglones
    # de canal automático / lista completa
    U = P * ((GRID[:, None] - c_fila[None, :]) * (monto * peso)[None, :])

    # agregación por SKU (promedios ponderados por peso)
    codigos, skus = pd.factorize(rec["sku"])
    n_skus = len(skus)
    util_sku = np.zeros((len(GRID), n_skus))
    pwin_sku = np.zeros((len(GRID), n_skus))
    suma_w = np.bincount(codigos, weights=peso, minlength=n_skus)
    for i in range(len(GRID)):
        util_sku[i] = np.bincount(codigos, weights=U[i], minlength=n_skus)
        pwin_sku[i] = (np.bincount(codigos, weights=P[i] * peso, minlength=n_skus)
                       / np.maximum(suma_w, 1e-9))

    info = rec.groupby("sku").agg(
        id_art=("id_art", "first"), id_asterisco=("id_asterisco", "first"),
        linea=("linea", lambda s: s.mode().iat[0] if s.notna().any() else ""),
        marca=("marca", lambda s: s.mode().iat[0] if s.notna().any() else ""),
        n_renglones_90d=("won", "size"),
        rel_actual=("rel_precio", "median"))
    # win-rate observado ponderado por peso (consistente con el modelo)
    wr = rec.groupby("sku").apply(
        lambda g: np.average(g["won"], weights=g["peso"]))
    info["win_rate_obs"] = wr
    info = info.loc[skus]

    escala_mes = 30.0 / VENTANA_DIAS
    filas = []
    for j, sku in enumerate(skus):
        c = c_sku.get(sku, np.nan)
        if not np.isfinite(c) or c <= 0.02 or c >= 0.98:
            continue
        r_act = float(info["rel_actual"].iloc[j])
        valido = (np.abs(GRID - r_act) <= MAX_MOV) & (GRID >= c + PISO_MARGEN)
        if not valido.any():
            continue
        i_opt = int(np.argmax(np.where(valido, util_sku[:, j], -np.inf)))
        i_act = int(np.argmin(np.abs(GRID - r_act)))
        delta = float(GRID[i_opt]) - r_act
        accion = "SUBIR" if delta > UMBRAL_ACCION else ("BAJAR" if delta < -UMBRAL_ACCION else "MANTENER")
        impacto = float(util_sku[i_opt, j] - util_sku[i_act, j]) * escala_mes
        if accion == "MANTENER":
            impacto = 0.0
        if r_act >= 0.98:
            # población de precio de lista completo (mucho canal web sin atención):
            # el "acantilado" del modelo ahí no es causal — no reclamar impacto
            accion = "REVISAR_LISTA"
            impacto = 0.0
        n_r = int(info["n_renglones_90d"].iloc[j])
        confianza = "alta" if n_r >= 100 else ("media" if n_r >= 50 else "baja")
        filas.append({
            "id_art": info["id_art"].iloc[j], "id_asterisco": info["id_asterisco"].iloc[j],
            "linea": info["linea"].iloc[j], "marca": info["marca"].iloc[j],
            "n_renglones_90d": int(info["n_renglones_90d"].iloc[j]),
            "win_rate_obs": round(float(info["win_rate_obs"].iloc[j]), 3),
            "rel_precio_actual": round(r_act, 3),
            "rel_precio_optimo": round(float(GRID[i_opt]), 3),
            "delta_pts": round(delta, 3), "accion": accion,
            "paso_sugerido_pts": round(float(np.clip(delta, -0.04, 0.04)), 3),
            "en_tope": bool(abs(delta) >= MAX_MOV - 0.015),
            "confianza": confianza,
            "costo_sobre_lista": round(float(c), 3),
            "pwin_actual": round(float(pwin_sku[i_act, j]), 3),
            "pwin_optimo": round(float(pwin_sku[i_opt, j]), 3),
            "utilidad_actual_mes": round(float(util_sku[i_act, j]) * escala_mes, 0),
            "utilidad_optima_mes": round(float(util_sku[i_opt, j]) * escala_mes, 0),
            "impacto_mes": round(impacto, 0),
        })

    recos = pd.DataFrame(filas).sort_values("impacto_mes", ascending=False)
    recos.to_csv("out/recomendaciones.csv", index=False)

    resumen = [
        f"SKUs evaluados: {len(recos)}",
        f"acciones: {recos['accion'].value_counts().to_dict()}",
        f"impacto total estimado (moneda de lista/mes): {recos['impacto_mes'].sum():,.0f}",
        f"delta_pts medio: {recos['delta_pts'].mean():+.3f}  (p25 {recos['delta_pts'].quantile(.25):+.2f} / p75 {recos['delta_pts'].quantile(.75):+.2f})",
        "", "== TOP 15 por impacto ==",
        recos.head(15).to_string(index=False),
        "", "== TOP 10 BAJAR ==",
        recos[recos.accion == "BAJAR"].head(10).to_string(index=False),
    ]
    with open("out/resumen_recos.txt", "w") as f:
        f.write("\n".join(resumen))
    print("\n".join(resumen[:4]), flush=True)
    print("OK: out/recomendaciones.csv, out/resumen_recos.txt", flush=True)


if __name__ == "__main__":
    sys.exit(main())
