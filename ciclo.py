# -*- coding: utf-8 -*-
"""Loop proyección→realidad por ciclo (T1.2, aprobado 2026-07-27).

Cada ciclo de 3 semanas:
  emitir : congela un SNAPSHOT INMUTABLE de las recomendaciones vigentes
           (proyección, bandas, confianza, clase de serie) en out/ciclos/.
           El snapshot es el compromiso del motor ANTES de ver el resultado —
           jamás se edita retroactivamente.
  cerrar : al cumplirse el ciclo (+3 semanas de datos en el panel), compara
           proyección vs realidad, ajustado por mercado:
             - SKUs SIN cambio aplicado: mide la calidad del pronóstico u0
               (WAPE del ciclo, cobertura de la banda p10–p90).
             - SKUs CON cambio aplicado (columna `aplicado`, cuando salgamos
               de fase de pruebas): mide la DECISIÓN (Δ utilidad real vs
               proyectada) contra los no aplicados / holdout como control.

Uso:
  ./.venv/bin/python ciclo.py emitir
  ./.venv/bin/python ciclo.py cerrar [ruta_snapshot]   (default: el más antiguo abierto)
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, "out", "ciclos")
SEM_CICLO = 3

COLS_SNAP = ["codigo", "direccion", "confianza", "clase_serie", "rol", "proveedor",
             "precio_actual", "precio_sugerido", "cambio_pct", "revisar",
             "u_sem_actual", "u_sem_proyectado", "u_sem_p10", "u_sem_p90",
             "utilidad_sem_mantener", "utilidad_sem_sugerido", "eps", "segmento",
             "n_clientes", "tipo_precio", "corte", "remate", "clasif_erp"]


def emitir():
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
    corte = pd.Timestamp(pan.semana.max())
    # sello de corte (auditoría 2026-07-30, C3)
    if "corte" in recos.columns and str(recos.corte.iloc[0]) != corte.date().isoformat():
        raise SystemExit(f"SELLO DE CORTE: recos al {recos.corte.iloc[0]} pero panel al "
                         f"{corte.date()} — re-corre escenarios.py antes de emitir")
    # un solo ciclo abierto a la vez (auditoría 2026-07-30, M12): checkpoint
    # vigila el más reciente y cerrar toma el más antiguo — no deben divergir
    prev = sorted(glob.glob(os.path.join(DIR, "ciclo_*.csv")))
    abiertos_prev = [r for r in prev if (pd.read_csv(r, nrows=1).estado == "abierto").all()]
    if abiertos_prev:
        raise SystemExit(f"ya hay un ciclo ABIERTO ({os.path.basename(abiertos_prev[0])}) — "
                         f"ciérralo antes de emitir otro")
    snap = recos[[c for c in COLS_SNAP if c in recos.columns]].copy()
    snap["fecha_emision"] = corte.date().isoformat()
    snap["fecha_cierre"] = (corte + pd.Timedelta(weeks=SEM_CICLO)).date().isoformat()
    snap["aplicado"] = False      # se marca True por SKU cuando el cambio se aplique en el ERP
    # T1.1: el holdout viene del sorteo de escenarios.py (15%, semilla=corte)
    snap["holdout"] = (recos.holdout.values if "holdout" in recos.columns
                       else False)
    snap["estado"] = "abierto"
    os.makedirs(DIR, exist_ok=True)
    ruta = os.path.join(DIR, f"ciclo_{corte.date().isoformat()}.csv")
    if os.path.exists(ruta):
        raise SystemExit(f"ya existe {ruta} — el snapshot es inmutable, no se re-emite")
    snap.to_csv(ruta, index=False)
    print(f"ciclo emitido: {ruta} ({len(snap):,} SKUs, datos al {corte.date()}, "
          f"cierre {snap.fecha_cierre.iloc[0]})", flush=True)
    return ruta


def _mercado(pan, ini, fin, prev_ini, prev_fin):
    """Factor de mercado: venta total del periodo del ciclo vs las 3 sem previas."""
    tot = pan.groupby("semana").unidades.sum()
    ahora = tot[(tot.index > ini) & (tot.index <= fin)].mean()
    antes = tot[(tot.index > prev_ini) & (tot.index <= prev_fin)].mean()
    return float(ahora / antes) if antes > 0 else 1.0


def cerrar(ruta=None):
    abiertos = sorted(glob.glob(os.path.join(DIR, "ciclo_*.csv")))
    if ruta is None:
        abiertos = [r for r in abiertos if (pd.read_csv(r, nrows=1).estado == "abierto").all()]
        if not abiertos:
            raise SystemExit("no hay ciclos abiertos")
        ruta = abiertos[0]
    snap = pd.read_csv(ruta)
    emision = pd.Timestamp(snap.fecha_emision.iloc[0])
    cierre = pd.Timestamp(snap.fecha_cierre.iloc[0])
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
    if pd.Timestamp(pan.semana.max()) < cierre:
        raise SystemExit(f"panel llega a {pan.semana.max()} y el ciclo cierra {cierre.date()} "
                         f"— re-extraer y reconstruir panel antes de cerrar")
    vent = pan[(pan.semana > emision) & (pan.semana <= cierre)]
    real = vent.groupby("codigo").unidades_rec.mean().rename("u_sem_real")
    m = snap.merge(real, on="codigo", how="left")
    m["u_sem_real"] = m.u_sem_real.fillna(0.0)
    fac = _mercado(pan, emision, cierre, emision - pd.Timedelta(weeks=SEM_CICLO), emision)
    print(f"cierre de {os.path.basename(ruta)} — mercado del ciclo: {fac:+.1%} vs 3 sem previas",
          flush=True)

    # 1) calidad del pronóstico (SKUs sin cambio aplicado: la proyección era 'mantener')
    sin = m[~m.aplicado]
    base = sin.u_sem_actual.clip(lower=0)
    wape = (base - sin.u_sem_real).abs().sum() / max(sin.u_sem_real.sum(), 1e-9)
    dentro = ((sin.u_sem_real >= sin.u_sem_p10) & (sin.u_sem_real <= sin.u_sem_p90)).mean()
    print(f"  pronóstico (n={len(sin):,}): WAPE del ciclo {wape:.3f} | "
          f"cobertura p10–p90: {100*dentro:.0f}% (objetivo ~80%)", flush=True)
    if "clase_serie" in m.columns:
        for cl, g in sin.groupby("clase_serie"):
            w = (g.u_sem_actual - g.u_sem_real).abs().sum() / max(g.u_sem_real.sum(), 1e-9)
            print(f"    {cl:<18} WAPE {w:.3f} | cobertura "
                  f"{100*((g.u_sem_real>=g.u_sem_p10)&(g.u_sem_real<=g.u_sem_p90)).mean():.0f}% "
                  f"(n={len(g):,})", flush=True)

    # 2) calidad de la DECISIÓN (cuando haya cambios aplicados)
    apl = m[m.aplicado]
    if len(apl):
        ctl = m[~m.aplicado & (m.direccion != "MANTENER")]
        # realizado vs proyectado, ambos lados ajustados por el mismo mercado
        d_real = (apl.u_sem_real - apl.u_sem_actual * fac)
        print(f"  decisión (n={len(apl):,} aplicados vs {len(ctl):,} control): "
              f"Δ unidades real vs esperado-sin-cambio: {d_real.sum():+,.0f}/sem", flush=True)
    else:
        print("  decisión: sin cambios aplicados este ciclo (fase de pruebas)", flush=True)

    m["estado"] = "cerrado"
    m["mercado_factor"] = round(fac, 4)
    ruta_out = ruta.replace("ciclo_", "cierre_")
    m.to_csv(ruta_out, index=False)
    # marcar el snapshot como cerrado SIN tocar sus proyecciones
    snap["estado"] = "cerrado"
    snap.to_csv(ruta, index=False)
    print(f"guardado {ruta_out}", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "emitir"
    if cmd == "emitir":
        emitir()
    elif cmd == "cerrar":
        cerrar(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        raise SystemExit("uso: ciclo.py [emitir|cerrar [ruta]]")
