# -*- coding: utf-8 -*-
"""Corre el pipeline completo del v3 en orden, con corte si un paso falla.

    panel.py → modelo.py → analisis_eps_sku.py aplicar → validar.py
    → forecast.py → analisis_canasta.py anclas → analisis_canal.py mezcla
    → escenarios.py

(la extracción NO se incluye: es lenta y se corre aparte con extract_api.py)

Uso:  ./.venv/bin/python run.py
"""
import subprocess
import sys
import time

PASOS = ["panel.py", "modelo.py", "validar.py", "forecast.py", "escenarios.py"]
# la capa SKU de elasticidad (EB por eventos, adoptada 2026-07-31 con juez OOT)
# se aplica entre modelo y escenarios
PASOS.insert(2, "analisis_eps_sku.py aplicar")
# mapa de anclas de canasta (regla aprobada 2026-07-31) antes de escenarios
PASOS.insert(5, "analisis_canasta.py anclas")
# mezcla de canal en línea/vendedor (aprobada 2026-08-01) antes de escenarios
PASOS.insert(6, "analisis_canal.py mezcla")


def main():
    t0 = time.time()
    for paso in PASOS:
        print(f"\n{'='*60}\n== {paso}\n{'='*60}", flush=True)
        r = subprocess.run([sys.executable] + paso.split())
        if r.returncode != 0:
            raise SystemExit(f"FALLÓ {paso} (exit {r.returncode}); pipeline detenido.")
    print(f"\nPIPELINE COMPLETO en {time.time()-t0:.0f}s — salidas en out/", flush=True)


if __name__ == "__main__":
    main()
