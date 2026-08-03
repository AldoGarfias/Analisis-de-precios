# -*- coding: utf-8 -*-
"""Precio óptimo por SKU a partir de la elasticidad estimada.

Utilidad semanal como función del multiplicador de precio de lista f = p1'/p1_actual:
    unidades(f) = u0 · f^ε
    margen_u(f) = rho·p1_actual·f − costo        (rho = realizado/lista, pass-through nivel)
    utilidad(f) = unidades(f) · margen_u(f)
Se barre f dentro de los guardrails y se toma el máximo. Elástico (|ε|>1) => hay
interior; inelástico (|ε|<1) => óptimo en el tope de subida.

Guardrails: piso margen costo+3pts, factor ≥1.6, movimiento máx ±10pts, paso mes ±4pts.
El override de sobrestock (no subir si meses≥12) lo aplica publicar.py con existencias.

Salida: out/recomendaciones.csv con el MISMO esquema que recommend.py (drop-in para
publicar.py). Las columnas pwin_* se reinterpretan como índice de volumen esperado.
"""
import os

import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "elast")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out")

MAX_MOV = 0.10          # movimiento máximo (puntos de lista)
PASO = 0.04             # paso prudente del mes
FACTOR_MIN = 1.6        # precio/costo mínimo
PISO_MARGEN = 1.03      # realizado ≥ costo × 1.03
UMBRAL = 0.005          # delta mínimo para accionar
ESCALA_MES = 4.33       # semanas/mes
MIN_SEM_VENTA = 8       # piso de actividad: ≥8 semanas con venta para opinar
COSTO_LISTA_MIN = 0.02  # costo/lista plausible (evita costos corruptos ~0)
COSTO_LISTA_MAX = 0.98  # costo/lista plausible (evita precio < costo por error)


def _optimo(u0, eps, p1, rho, costo):
    """Devuelve (f_opt, util_act_sem, util_opt_sem) barriendo f en guardrails."""
    lo_factor = FACTOR_MIN * costo / p1                 # piso por factor sobre lista
    lo_margen = (PISO_MARGEN * costo / rho) / p1        # piso por margen sobre realizado
    lo = max(1.0 - MAX_MOV, lo_factor, lo_margen)
    hi = 1.0 + MAX_MOV
    if lo > hi:
        lo = hi = 1.0                                    # sin espacio: mantener
    grid = np.round(np.arange(lo, hi + 1e-9, 0.005), 4)
    if lo <= 1.0 <= hi and 1.0 not in grid:
        grid = np.append(grid, 1.0)  # ofrecer "no cambio" sólo si es factible
    unidades = u0 * grid ** eps
    margen = rho * p1 * grid - costo
    util = unidades * np.clip(margen, 0, None)
    j = int(np.argmax(util))
    util_act = u0 * (rho * p1 - costo)
    return float(grid[j]), float(max(util_act, 0.0)), float(util[j])


def generar():
    base = pd.read_parquet(f"{DATA}/baseline.parquet")
    modelo = pd.read_parquet(f"{DATA}/modelo.parquet")
    df = base.merge(modelo[["id_art", "eps", "eps_se", "identificado", "confianza",
                            "fuente"]], on="id_art", how="left")

    filas = []
    for r in df.itertuples(index=False):
        p1 = float(r.p1_actual or r.precio_1 or 0)
        costo = float(r.costo or 0)
        rho = float(r.rho) if r.rho and r.rho > 0 else 0.9
        u0 = float(r.unidades_prom_sem or 0)
        eps = float(r.eps) if pd.notna(r.eps) else np.nan
        ident = bool(r.identificado) and pd.notna(eps)
        c_lista = costo / p1 if p1 > 0 else 0.0
        sem_venta = int(r.semanas_venta or 0)

        # Guardas de accionabilidad (heredadas del v1):
        #  - piso de actividad (≥8 semanas con venta) -> no opinar sobre ruido
        #  - costo/lista plausible -> no accionar sobre costos corruptos (~0)
        #  - kits NO se filtran aquí (col cat_articulos2.kit no es flag confiable);
        #    los bloquea el endpoint cambiar-precios al aplicar.
        accionable = (ident and p1 > 0 and costo > 0 and u0 > 0
                      and sem_venta >= MIN_SEM_VENTA
                      and COSTO_LISTA_MIN <= c_lista <= COSTO_LISTA_MAX)
        if accionable:
            f_opt, util_act, util_opt = _optimo(u0, eps, p1, rho, costo)
        else:
            f_opt, util_act, util_opt = 1.0, max(u0 * (rho * p1 - costo), 0.0), 0.0

        delta = round(f_opt - 1.0, 3)
        if not accionable or abs(delta) < UMBRAL:
            accion, delta, f_opt = "MANTENER", 0.0, 1.0
            impacto = 0.0
        else:
            accion = "SUBIR" if delta > 0 else "BAJAR"
            impacto = round((util_opt - util_act) * ESCALA_MES, 0)

        paso = round(float(np.clip(delta, -PASO, PASO)), 3)
        vol_opt = round(float(f_opt ** eps), 3) if ident else 1.0

        filas.append({
            "id_art": int(r.id_art), "id_asterisco": 0,
            "linea": r.linea, "marca": r.marca,
            "n_renglones_90d": int(r.semanas_venta or 0),
            "win_rate_obs": round(float((r.semanas_venta or 0) / max(int(r.semanas or 1), 1)), 3),
            "rel_precio_actual": 1.0,
            "rel_precio_optimo": round(float(f_opt), 3),
            "delta_pts": delta, "accion": accion,
            "paso_sugerido_pts": paso,
            "en_tope": bool(abs(delta) >= MAX_MOV - 0.005),
            "confianza": r.confianza if isinstance(r.confianza, str) else "baja",
            "costo_sobre_lista": round(costo / p1, 3) if p1 > 0 else 0.0,
            "pwin_actual": 1.0, "pwin_optimo": vol_opt,
            "utilidad_actual_mes": round(util_act * ESCALA_MES, 0),
            "utilidad_optima_mes": round(util_opt * ESCALA_MES, 0),
            "impacto_mes": impacto,
            "eps": round(eps, 3) if ident else None,   # extra (publicar.py lo ignora)
        })

    recos = pd.DataFrame(filas).sort_values("impacto_mes", ascending=False)
    os.makedirs(OUT, exist_ok=True)
    recos.drop(columns=["eps"]).to_csv(f"{OUT}/recomendaciones.csv", index=False)
    recos.to_parquet(f"{DATA}/recos_full.parquet", index=False)

    cols_top = ["id_art", "marca", "delta_pts", "eps", "impacto_mes"]
    resumen = [
        f"SKUs evaluados: {len(recos)}",
        f"acciones: {recos['accion'].value_counts().to_dict()}",
        f"impacto total estimado (USD/mes): {recos['impacto_mes'].sum():,.0f}",
        f"confianza: {recos['confianza'].value_counts().to_dict()}",
        "", "== TOP 10 SUBIR ==",
        recos[recos.accion == "SUBIR"].head(10)[cols_top].to_string(index=False),
        "", "== TOP 10 BAJAR ==",
        recos[recos.accion == "BAJAR"].head(10)[cols_top].to_string(index=False),
    ]
    with open(f"{OUT}/resumen_recos_elast.txt", "w") as f:
        f.write("\n".join(resumen))
    print("\n".join(resumen[:4]), flush=True)
    return recos


if __name__ == "__main__":
    generar()
