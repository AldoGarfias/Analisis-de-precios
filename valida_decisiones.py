# -*- coding: utf-8 -*-
"""Calibración EMPÍRICA de decisiones de precio contra la historia real.

Responde: de los cambios de precio que el negocio YA hizo (la ventana vigente del panel (2024→hoy), miles de
eventos), ¿qué fracción terminó con MÁS utilidad que mantener? Esa tasa de
éxito observada es la confiabilidad que INCLUYE el riesgo de modelo completo
(forma funcional, pass-through, competencia, heterogeneidad): no sale de
supuestos, sale de lo que pasó.

Método (replay por evento):
  1. Detectar cambios de lista ≥1.5% con ≥8 semanas de historia antes y ≥3 después
     (3 semanas = el horizonte real de cada decisión del motor).
  2. Real:          utilidad de las 3 semanas posteriores (venta recurrente ×
                    margen neto observado después del cambio).
  3. Contrafactual: venta previa (8 sem) ajustada por el MERCADO de las mismas
                    3 semanas × margen neto previo — "qué habría pasado sin tocar".
  4. Éxito = utilidad real > utilidad contrafactual.
  5. Se descartan eventos sin stock disponible después (la caída sería por
     disponibilidad) y con venta previa < 5 u/sem (sin señal).

Salida: data/calibracion_decisiones.parquet (tasa de éxito por dirección ×
magnitud) — el reporte la usa como "confiabilidad empírica".
"""
import os

import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

BUCKETS = [(0.015, 0.035, "2-3%"), (0.035, 0.055, "4-5%"),
           (0.055, 0.09, "6-8%"), (0.09, 1.0, ">9%")]


def _bucket(mag):
    for lo, hi, lbl in BUCKETS:
        if lo <= mag < hi:
            return lbl
    return None


def correr():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
    semanas = np.sort(pan.semana.unique())
    u = pan.pivot(index="codigo", columns="semana", values="unidades").reindex(columns=semanas).fillna(0.0)
    p = pan.pivot(index="codigo", columns="semana", values="precio_lista").reindex(columns=semanas).ffill(axis=1)
    neto = pan.pivot(index="codigo", columns="semana", values="neto_prom").reindex(columns=semanas).ffill(axis=1)
    cst = pan.pivot(index="codigo", columns="semana", values="costo_prom").reindex(columns=semanas).ffill(axis=1)
    # T2.3 (2026-07-27) — CONTROLES LIMPIOS: el factor de mercado se calcula
    # SOLO con observaciones de SKUs SIN cambio de precio reciente (±H sem).
    # Antes el "mercado" incluía a los propios tratados ⇒ comparación
    # contaminada (la falla TWFE que documenta Callaway–Sant'Anna).
    pv = p.values
    chg = np.zeros(u.shape, dtype=bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(pv[:, 1:] / np.where(pv[:, :-1] > 0, pv[:, :-1], np.nan) - 1)
    chg[:, 1:] = rel >= 0.015
    tratado = np.zeros_like(chg)
    HW = 3
    for k in range(-HW, HW + 1):
        if k >= 0:
            tratado[:, k:] |= chg[:, :chg.shape[1] - k]
        else:
            tratado[:, :k] |= chg[:, -k:]
    u_limpio = np.where(tratado, 0.0, u.values)
    mkt = u_limpio.sum(axis=0)
    print(f"  controles limpios: {100*tratado.mean():.0f}% de celdas SKU-semana "
          f"excluidas del mercado por tratamiento reciente", flush=True)

    ruta_ex = os.path.join(DATA, "reporte61", "existencias_sem.parquet")
    disp = None
    if os.path.exists(ruta_ex):
        ex = pd.read_parquet(ruta_ex)
        col = "disp_venta" if "disp_venta" in ex.columns else "existencia"
        disp = (ex.pivot_table(index="codigo", columns="semana", values=col, aggfunc="last")
                .reindex(columns=semanas).ffill(axis=1))

    H = 3  # horizonte de evaluación = ciclo de decisión
    eventos = []
    n_rtm = 0
    for cod in u.index:
        pr, uv = p.loc[cod].values, u.loc[cod].values
        nv, cv = neto.loc[cod].values, cst.loc[cod].values
        dv = disp.loc[cod].values if (disp is not None and cod in disp.index) else None
        for i in range(8, len(pr) - H):
            if not (np.isfinite(pr[i - 1]) and pr[i - 1] > 0 and np.isfinite(pr[i])):
                continue
            mag = pr[i] / pr[i - 1] - 1
            if abs(mag) < 0.015:
                continue
            u_antes = uv[i - 8:i].mean()
            if u_antes < 5:
                continue
            # T2.3 — PRE-TRENDS (anti regresión a la media): si la demanda del
            # SKU ya venía divergiendo ±30% vs mercado ANTES del cambio, el
            # cambio siguió a un shock propio y lo que venga después es en gran
            # parte retorno a la media, no efecto del precio ⇒ fuera.
            u_pre1, u_pre2 = uv[i - 8:i - 4].mean(), uv[i - 4:i].mean()
            m_pre1, m_pre2 = mkt[i - 8:i - 4].mean(), mkt[i - 4:i].mean()
            if u_pre1 > 0 and m_pre1 > 0 and m_pre2 > 0:
                rel_pre = (u_pre2 / u_pre1) / (m_pre2 / m_pre1)
                if not (0.7 <= rel_pre <= 1.3):
                    n_rtm += 1
                    continue
            if dv is not None:
                d_desp = dv[i:i + H]
                d_desp = d_desp[np.isfinite(d_desp)]
                if len(d_desp) and np.nanmean(d_desp) <= 0:
                    continue  # sin stock después: no informa de la decisión
            m_antes, m_desp = mkt[i - 8:i].mean(), mkt[i:i + H].mean()
            if m_antes <= 0:
                continue
            u_desp = uv[i:i + H].mean()
            u_cf = u_antes * (m_desp / m_antes)              # contrafactual (mantener)
            neto_antes = np.nanmedian(nv[i - 8:i])
            costo_desp = np.nanmedian(cv[i:i + H])
            marg_desp = np.nanmedian(nv[i:i + H] - cv[i:i + H])
            # margen del contrafactual: si NO se hubiera tocado el precio, el neto
            # seguiría en el nivel previo pero el COSTO sería el nuevo (mantener no
            # te salva de un aumento de costo). Usar el margen viejo completo
            # sobreestima el contrafactual y castiga injustamente a las subidas.
            marg_cf = neto_antes - costo_desp
            if not (np.isfinite(marg_cf) and np.isfinite(marg_desp)):
                continue
            util_real = u_desp * marg_desp
            util_cf = u_cf * marg_cf
            eventos.append({"codigo": cod, "mag": mag,
                            "direccion": "subida" if mag > 0 else "bajada",
                            "bucket": _bucket(abs(mag)),
                            "exito": bool(util_real > util_cf),
                            "util_real": util_real, "util_cf": util_cf,
                            "d_util_rel": (util_real - util_cf) / max(abs(util_cf), 1e-9)})

    ev = pd.DataFrame(eventos).dropna(subset=["bucket"])
    print(f"eventos evaluables (cambios reales con historia y stock): {len(ev):,} "
          f"en {ev.codigo.nunique():,} SKUs | excluidos por pre-tendencia (RTM): "
          f"{n_rtm:,}", flush=True)

    res = (ev.groupby(["direccion", "bucket"])
           .agg(n=("exito", "size"), tasa_exito=("exito", "mean"),
                d_util_mediano=("d_util_rel", "median"),
                util_real_tot=("util_real", "sum"),
                util_cf_tot=("util_cf", "sum"))
           .reset_index())
    # efecto PORTAFOLIO: la suma de todos los eventos del bucket (el ruido por
    # evento se cancela; lo que queda es el efecto sistemático)
    res["uplift_portafolio"] = res.util_real_tot / res.util_cf_tot - 1
    print("\n== CALIBRACIÓN OBSERVADA (real vs contrafactual, a 3 semanas) ==", flush=True)
    for _, r in res.sort_values(["direccion", "bucket"]).iterrows():
        print(f"  {r.direccion:<7} {r.bucket:<5}: gana {100*r.tasa_exito:.0f}% de {r.n:,} eventos | "
              f"Δ mediano {100*r.d_util_mediano:+.1f}% | "
              f"PORTAFOLIO {100*r.uplift_portafolio:+.1f}%", flush=True)

    res.to_parquet(os.path.join(DATA, "calibracion_decisiones.parquet"), index=False)
    print(f"\nguardado data/calibracion_decisiones.parquet", flush=True)
    return res


if __name__ == "__main__":
    correr()
