# -*- coding: utf-8 -*-
"""Orquesta el motor de elasticidad end-to-end.

Uso:
  python3 run.py                      # marcas piloto, extrae de BD y corre todo
  python3 run.py HIKVISION,UBIQUITI   # marcas específicas
  python3 run.py --skip-extract       # reusa parquet existentes (sin tocar BD)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extract_ventas
import clusters
import panel
import modelo
import optimizar
import validar
from db import conectar_erp


def main():
    args = [a for a in sys.argv[1:]]
    skip = "--skip-extract" in args
    marcas_arg = [a for a in args if not a.startswith("--")]
    marcas = marcas_arg[0].split(",") if marcas_arg else extract_ventas.MARCAS_DEFAULT

    t0 = time.time()
    if not skip:
        print(f"[1/6] extracción (marcas={marcas}) ...", flush=True)
        con = conectar_erp()
        try:
            extract_ventas.extraer_todo(con, marcas)
        finally:
            con.close()
    else:
        print("[1/6] extracción OMITIDA (--skip-extract)", flush=True)

    print("[2/6] clusters ...", flush=True)
    clusters.construir()
    print("[3/6] panel ...", flush=True)
    panel.construir()
    print("[4/6] modelo (elasticidad) ...", flush=True)
    modelo.estimar()
    print("[5/6] optimizar ...", flush=True)
    optimizar.generar()
    print("[6/6] validar ...", flush=True)
    validar.validar()
    print(f"\nLISTO en {time.time()-t0:.0f}s. Salida: out/recomendaciones.csv", flush=True)


if __name__ == "__main__":
    main()
