# -*- coding: utf-8 -*-
"""Genera data/reporte61/codigos_activos.parquet: códigos SYSCOM con venta
en los últimos N meses (default 3 completos). Se usa para restringir el matching
por TEXTO (Capas 4/5) a modelos que SE VENDEN hoy, no a todo el catálogo.
"""
import glob
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data", "reporte61")


def generar(meses_activos=3):
    """Códigos con venta en los últimos N meses completos disponibles."""
    rutas = sorted(glob.glob(os.path.join(DATA, "ventas_*.parquet")))
    fechas = [os.path.basename(r)[7:-8] for r in rutas]  # '2026_07'
    fechas = sorted(fechas)
    usar = fechas[-meses_activos:]
    partes = []
    for f in usar:
        d = pd.read_parquet(os.path.join(DATA, f"ventas_{f}.parquet"),
                            columns=["codigo"])
        partes.append(d["codigo"].dropna().astype(str))
    todo = pd.concat(partes)
    activos = todo.drop_duplicates().rename("codigo").to_frame()
    activos = activos[activos.codigo.str.strip().ne("")]
    ruta = os.path.join(DATA, "codigos_activos.parquet")
    activos.to_parquet(ruta, index=False)
    print(f"codigos_activos.parquet: {len(activos):,} códigos "
          f"(venta en últimos {meses_activos} meses: {usar[0]}…{usar[-1]})",
          flush=True)


if __name__ == "__main__":
    from datetime import datetime
    generar(int(sys.argv[1]) if len(sys.argv) > 1 else 3)