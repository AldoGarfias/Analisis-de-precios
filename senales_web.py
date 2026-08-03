# -*- coding: utf-8 -*-
"""SEÑALES WEB del embudo de syscom.mx → contexto para el motor (2026-07-29).

De la única tabla visible en la API de BI (v_bi_eventos_interacciones) se
extrae, por código del motor: vistas, clicks, carritos y compras web de la
ventana disponible, y la CONVERSIÓN vista→compra.

USO EN EL MOTOR: INFORMATIVO (no decide) — la serie nació el 2026-07-23; con
3-4 semanas acumuladas se evaluará si alguna señal merece entrar a reglas
(con aprobación). Mientras: columnas web_* en recomendaciones.csv, chips en el
reporte y el renglón "escaparate web" en el detallado.

Mecánica: los 12.6K códigos del motor se mapean a id_producto (cat_modelos) y
se consultan por lotes (la API regresa máx 1000 filas y 30 requests/min).

Uso:  ./.venv/bin/python senales_web.py
Salida: data/senales_web.parquet
"""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_bi import q  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
LOTE = 250          # ids por consulta (≤1000 filas garantizado)
PAUSA = 2.1         # seg entre consultas (30 req/min del servidor)


def correr():
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    cat = pd.read_csv(os.path.expanduser("~/Downloads/cat_modelos.csv"))
    cat["modelo"] = cat.modelo.astype(str).str.strip()
    mapa = cat[cat.modelo.isin(recos.codigo)].drop_duplicates("id_producto")
    ids = mapa.id_producto.tolist()
    print(f"códigos del motor con id_producto: {len(ids):,} de {len(recos):,}", flush=True)

    rango = q("SELECT MIN(fecha) AS d, MAX(fecha) AS h FROM v_bi_eventos_interacciones LIMIT 1")
    desde, hasta = pd.Timestamp(rango.d[0]), pd.Timestamp(rango.h[0])
    dias = max((hasta - desde).days, 1)

    partes = []
    t0 = time.time()
    for i in range(0, len(ids), LOTE):
        sub = ids[i:i + LOTE]
        lista = ",".join(str(x) for x in sub)
        df = q(f"SELECT id_producto, SUM(tipo='vista') AS vistas, "
               f"SUM(tipo='click') AS clicks, SUM(tipo='add_carrito') AS carritos, "
               f"SUM(tipo='compra') AS compras FROM v_bi_eventos_interacciones "
               f"WHERE id_producto IN ({lista}) GROUP BY id_producto LIMIT 1000")
        if len(df):
            partes.append(df)
        if (i // LOTE) % 10 == 0:
            print(f"  lote {i//LOTE + 1}/{(len(ids)-1)//LOTE + 1} "
                  f"({time.time()-t0:.0f}s)", flush=True)
        time.sleep(PAUSA)
    web = pd.concat(partes, ignore_index=True)
    for c in ["vistas", "clicks", "carritos", "compras"]:
        web[c] = web[c].astype(float)
    web = web.merge(mapa[["id_producto", "modelo"]], on="id_producto")
    web = (web.groupby("modelo", as_index=False)
           [["vistas", "clicks", "carritos", "compras"]].sum()
           .rename(columns={"modelo": "codigo"}))
    web["conv_pct"] = (100 * web.compras / web.vistas.clip(lower=1)).round(1)
    web["dias_ventana"] = dias
    web["corte"] = hasta.date().isoformat()
    ruta = os.path.join(BASE, "data", "senales_web.parquet")
    web.to_parquet(ruta, index=False)
    con_traf = (web.vistas >= 50).sum()
    print(f"\nguardado {ruta}: {len(web):,} códigos con actividad web "
          f"({con_traf:,} con ≥50 vistas) | ventana {dias} días al {hasta.date()}", flush=True)
    print(f"conversión mediana (≥100 vistas): "
          f"{web[web.vistas >= 100].conv_pct.median():.1f}%", flush=True)


if __name__ == "__main__":
    correr()
