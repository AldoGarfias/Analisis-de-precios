# -*- coding: utf-8 -*-
"""CACHE NOCTURNO DE DETALLADOS — todos los modelos, todos los días (usuario
2026-07-31: "los detallados se deberían actualizar todos los días a las 0:00,
generar el cache, para tenerlos disponibles al momento de consulta").

Genera el panel detallado STANDALONE de:
  - TODOS los modelos evaluables del motor (out/recomendaciones.csv), y
  - los dormidos de la 2ª capa con diagnóstico (out/segunda_capa_dormidos.csv).

Salida: out/paneles/panel_<CODIGO>.html (uno por modelo, autocontenido) +
out/paneles/index.html (buscador simple client-side). ~19 ms por panel ⇒ el
catálogo completo toma ~4-5 min.

El cache refleja la última corrida del pipeline (recos/panel) MÁS lo que
cambia a diario (bloque de movimiento de costo de la vigía, registro de
frenos), por eso se regenera cada noche aunque el ciclo no haya corrido.

Programado en cron a las 0:00 (marca # motor-precios-paneles-cache).
Uso manual:  ./.venv/bin/python genera_paneles.py
"""
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from panel_sku import (BASE, ESTILOS, JS_LIB, PIE, _safe, cargar_ctx,   # noqa: E402
                       cuerpo_dormido, cuerpo_sku)

DIR = os.path.join(BASE, "out", "paneles")


def _escribe(sid, titulo, cuerpo, D):
    html = (f'<meta charset="utf-8">\n<title>{titulo}</title>\n' + ESTILOS
            + f'\n<div class="wrap">{cuerpo}<div class="foot">{PIE} · cache nocturno</div></div>'
            + '\n<div class="tip" id="tip"></div>'
            + f'\n<script>{JS_LIB}\nrenderSku({json.dumps(D, ensure_ascii=False)});</script>')
    with open(os.path.join(DIR, f"panel_{sid}.html"), "w", encoding="utf-8") as f:
        f.write(html)


def correr():
    os.makedirs(DIR, exist_ok=True)
    t0 = time.time()
    ctx = cargar_ctx()
    recos = ctx["recos"]
    ok = err = 0
    indice = []
    for cod in recos.codigo:
        try:
            cuerpo, D = cuerpo_sku(cod, ctx, volver_html="")
            _escribe(_safe(cod), f"{cod} — detalle · Motor de Precio Óptimo v3", cuerpo, D)
            indice.append((cod, "motor"))
            ok += 1
        except Exception as e:
            err += 1
            if err <= 5:
                print(f"  err {cod}: {str(e)[:60]}", flush=True)
    # dormidos 2ª capa (con diagnóstico y precio de época viva)
    ruta_d = os.path.join(BASE, "out", "segunda_capa_dormidos.csv")
    if os.path.exists(ruta_d):
        dd = pd.read_csv(ruta_d)
        ruta_ex = os.path.join(BASE, "data", "reporte61", "existencias_sem.parquet")
        exist_d = pd.read_parquet(ruta_ex) if os.path.exists(ruta_ex) else None
        for _, xd in dd.dropna(subset=["precio_epoca_viva"]).iterrows():
            try:
                cuerpo, D = cuerpo_dormido(xd.codigo, ctx, xd, exist=exist_d, volver_html="")
                _escribe(_safe(xd.codigo), f"{xd.codigo} — dormido · Motor de Precio Óptimo v3",
                         cuerpo, D)
                indice.append((xd.codigo, "dormido"))
                ok += 1
            except Exception:
                err += 1
    # índice con buscador
    filas = "".join(f'<option value="panel_{_safe(c)}.html">{c} ({t})</option>'
                    for c, t in sorted(indice))
    idx = (f'<meta charset="utf-8"><title>Detallados — cache nocturno</title>{ESTILOS}'
           f'<div class="wrap"><div class="card" style="padding:16px">'
           f'<h2 style="margin:0 0 8px">Detallados por modelo ({len(indice):,})</h2>'
           f'<div class="sec-s">cache del {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")} — '
           f'escribe o elige un código y se abre su panel</div>'
           f'<input class="btn" list="cods" id="q" placeholder="Código…" '
           f'style="min-width:280px" onchange="var v=this.value;if(v)location.href=v">'
           f'<datalist id="cods">{filas}</datalist></div></div>')
    with open(os.path.join(DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(idx)
    print(f"cache de detallados: {ok:,} paneles ({err} errores) en "
          f"{(time.time()-t0)/60:.1f} min → {DIR}", flush=True)


if __name__ == "__main__":
    correr()
