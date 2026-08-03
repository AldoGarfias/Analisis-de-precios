# -*- coding: utf-8 -*-
"""CHECKPOINT SEMANAL del ciclo — detecta movimientos que afectan las
recomendaciones vigentes ANTES del cierre del ciclo (usuario 2026-07-30:
"conviene cachear los cálculos de ciclo cada semana, para detectar ventas o
movimiento que afecten nuestras recomendaciones").

Cada semana (lunes, encadenado al cron diario) para el CICLO ABIERTO:
  1. VENTA REAL semana a semana desde la emisión del ciclo (de los parquets de
     ventas, con los filtros del panel: tipo_precio 1/3) vs la banda p10-p90
     que el motor proyectó al emitir. Se CACHEA en out/checkpoints/ para que
     el cierre del ciclo y las autopsias tengan la foto de cada semana.
  2. BANDERAS por SKU con recomendación activa (SUBIR/BAJAR no aplicadas o en
     medición):
       · DEMANDA SE DESPLOMÓ — venta bajo p10 en las semanas transcurridas
         (⇒ revisar antes de aplicar un SUBIR)
       · PICO ATÍPICO — venta sobre p90 (¿proyecto/cliente grande? ⇒ la
         medición del ciclo queda contaminada; revisar antes de leer)
       · STOCKOUT — sin stock hoy (vigía diaria): no se puede medir ni aplicar
       · COSTO MOVIÓ — el costo cambió ≥2% desde la emisión (vigía): el margen
         que justificó la decisión ya no es el mismo
  3. Resumen impreso + notificación macOS si hay banderas nuevas.

NO re-decide nada: las banderas son para revisar; el árbol de decisión corre
solo al cierre del ciclo con el corte congelado (filosofía campeón-retador).

Uso:  ./.venv/bin/python checkpoint_semanal.py
Salida: out/checkpoints/chk_YYYY-MM-DD.csv (cache semanal, inmutable)
        out/checkpoint_banderas.csv (banderas vigentes, sobrescrito)
"""
import glob
import os
import subprocess
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, "out", "checkpoints")


def _ciclo_abierto():
    rutas = sorted(glob.glob(os.path.join(BASE, "out", "ciclos", "ciclo_*.csv")))
    if not rutas:
        return None
    c = pd.read_csv(rutas[-1])
    if "estado" in c.columns and (c.estado == "cerrado").all():
        return None
    return c


def _ventas_semanales(desde):
    """Unidades por codigo × semana desde `desde`, con filtros del panel."""
    meses = pd.period_range(desde, pd.Timestamp.today(), freq="M")
    partes = []
    for p in meses:
        ruta = os.path.join(BASE, "data", "reporte61", f"ventas_{p.year}_{p.month:02d}.parquet")
        if os.path.exists(ruta):
            df = pd.read_parquet(ruta, columns=["fecha", "codigo", "cantidad", "tipo_precio"])
            partes.append(df)
    v = pd.concat(partes, ignore_index=True)
    v = v[v.tipo_precio.isin([1, 3])]
    f = pd.to_datetime(v.fecha)
    if f.dt.tz is not None:
        f = f.dt.tz_localize(None)
    v["semana"] = f.dt.to_period("W-SUN").dt.start_time  # semanas lunes-domingo
    fecha_max = f.max().normalize()
    v = v[v.semana >= pd.Timestamp(desde)]
    return v.groupby(["codigo", "semana"], as_index=False).cantidad.sum(), fecha_max


def correr():
    ciclo = _ciclo_abierto()
    if ciclo is None:
        print("checkpoint: no hay ciclo abierto", flush=True)
        return None
    emision = pd.Timestamp(ciclo.fecha_emision.iloc[0])
    hoy = pd.Timestamp.today().normalize()
    # solo semanas COMPLETAS transcurridas desde la emisión
    ini_sem = emision.to_period("W-SUN").start_time
    vs, fecha_max = _ventas_semanales(ini_sem)
    fin_completa = hoy.to_period("W-SUN").start_time  # la semana en curso no cuenta
    # FRESCURA (auditoría 2026-07-30, C6): solo semanas CUBIERTAS por la
    # extracción — una semana parcial daría falsas "DEMANDA BAJO p10" grabadas
    # en cache inmutable. Si la extracción está atrasada, se recorta y se avisa.
    fin_cubierta = (fecha_max + pd.Timedelta(days=1)).to_period("W-SUN").start_time
    if fin_cubierta < fin_completa:
        print(f"  ⚠ extracción de ventas al {fecha_max.date()}: se evalúa solo hasta la "
              f"semana del {(fin_cubierta - pd.Timedelta(weeks=1)).date()} — corre "
              f"extract.py para cubrir la semana pasada", flush=True)
        fin_completa = fin_cubierta
    vs = vs[vs.semana < fin_completa]
    sem_transcurridas = sorted(vs.semana.unique())
    if not sem_transcurridas:
        print("checkpoint: aún no hay semanas completas del ciclo", flush=True)
        return None

    piv = vs.pivot(index="codigo", columns="semana", values="cantidad")
    piv = piv.reindex(ciclo.codigo).fillna(0.0)
    chk = ciclo[["codigo", "direccion", "confianza", "u_sem_actual", "u_sem_proyectado",
                 "u_sem_p10", "u_sem_p90", "precio_actual", "precio_sugerido",
                 "aplicado", "holdout"]].copy()
    chk["u_real_prom"] = piv.mean(axis=1).values
    for i, s in enumerate(sem_transcurridas, 1):
        chk[f"sem{i}"] = piv[s].values
    chk["n_sem"] = len(sem_transcurridas)

    # banderas (solo recomendaciones activas de mover precio)
    activa = chk.direccion.isin(["SUBIR", "BAJAR"])
    chk["bandera"] = ""
    desplome = activa & (chk.u_real_prom < chk.u_sem_p10)
    pico = activa & (chk.u_real_prom > chk.u_sem_p90)
    chk.loc[desplome, "bandera"] = "DEMANDA BAJO p10 — revisar antes de aplicar/leer"
    chk.loc[pico, "bandera"] = "PICO SOBRE p90 — ¿proyecto? medición contaminada"

    # stock y costo desde la vigía diaria (si ya hay snapshots)
    snaps = sorted(glob.glob(os.path.join(BASE, "data", "vigia", "snap_*.parquet")))
    if snaps:
        v_hoy = pd.read_parquet(snaps[-1]).set_index("codigo")
        chk["stock_hoy"] = chk.codigo.map(v_hoy.existencia)
        so = activa & (chk.stock_hoy <= 0)
        chk.loc[so, "bandera"] = (chk.loc[so, "bandera"] + " · ").str.replace(r"^ · $", "", regex=True) \
            + "STOCKOUT — no se puede medir/aplicar"
        if len(snaps) >= 2:
            # línea base = el snapshot más CERCANO a la emisión del ciclo (M5),
            # no el primero de la historia de la vigía
            def _f(r):
                return pd.Timestamp(os.path.basename(r)[5:15])
            base = min(snaps, key=lambda r: abs((_f(r) - emision).days))
            v0 = pd.read_parquet(base).set_index("codigo")
            d_costo = (chk.codigo.map(v_hoy.costo_prov) / chk.codigo.map(v0.costo_prov) - 1)
            movio = activa & (d_costo.abs() >= 0.02)
            chk.loc[movio, "bandera"] = np.where(
                chk.loc[movio, "bandera"] == "", "", chk.loc[movio, "bandera"] + " · ") \
                + "COSTO MOVIÓ " + (100 * d_costo[movio]).round(1).astype(str) + "%"

    os.makedirs(DIR, exist_ok=True)
    ruta = os.path.join(DIR, f"chk_{hoy.date().isoformat()}.csv")
    chk.to_csv(ruta, index=False)
    banderas = chk[chk.bandera != ""]
    banderas.to_csv(os.path.join(BASE, "out", "checkpoint_banderas.csv"), index=False)
    dentro = ((chk.u_real_prom >= chk.u_sem_p10) & (chk.u_real_prom <= chk.u_sem_p90)).mean()
    print(f"checkpoint {hoy.date()}: {len(sem_transcurridas)} semana(s) del ciclo | "
          f"venta dentro de banda: {100*dentro:.0f}% | banderas: {len(banderas):,} "
          f"({banderas.bandera.str.split(' — ').str[0].value_counts().to_dict()})", flush=True)
    print(f"  cache → {ruta} | banderas → out/checkpoint_banderas.csv", flush=True)
    if len(banderas):
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "Checkpoint del ciclo: {len(banderas)} '
                            f'recomendaciones con bandera (ver checkpoint_banderas.csv)" '
                            f'with title "Motor de Precio v3"'], check=False, timeout=10)
        except Exception:
            pass
    return chk


if __name__ == "__main__":
    correr()
