# -*- coding: utf-8 -*-
"""¿QUÉ FUNCIONA PARA REVIVIR UN MODELO? — estudio de eventos sobre TODA la
historia (usuario 2026-07-31: "¿qué comportamiento ves en todos los datos?
¿qué pasó cuando no teníamos stock y llegó nuevo — funcionó bajar, mantener o
subir? identifica escenarios para recomendar; si el SKU no alcanza, agrupa
por proveedor o algo que haga sentido").

Dos familias de eventos, panel completo (todas las series con historia):

  A. REABASTO: ≥3 semanas sin stock vendible → llega stock. ¿Con qué precio
     se recibió al stock nuevo (vs la lista pre-stockout)? BAJÓ (≤−3%) /
     IGUAL (±3%) / SUBIÓ (≥+3%). Resultado: recuperación de la venta en las
     6 semanas siguientes vs su línea base pre-stockout, ajustada por mercado,
     y semanas hasta la primera venta.

  B. DORMIDO CON STOCK: ≥8 semanas sin venta recurrente TENIENDO stock.
     ¿Se bajó la lista (≥5%) durante el silencio, o se dejó igual?
     Resultado: tasa de RESURRECCIÓN (≥2 semanas con venta, o ≥3 unidades,
     en las 8 semanas posteriores a la acción/ancla) y profundidad del
     recorte que mejor revive (5-10 / 10-20 / >20%).

Robustez: resultados globales + por PROVEEDOR (donde n alcanza) + por
segmento de rotación. Salidas: data/analisis_reactivacion_A.parquet
(reabastos) y _B.parquet (silencios), evento por evento + impresión.

OJO: dormidos.py YA DERIVA REGLAS de estos parquets (adoptadas 2026-07-31):
el precio de REABASTECIDO (recibir a precio vigente), la evidencia de grupo
por proveedor para el recorte de la cadencia (Δ≥+10pts, n≥20 por brazo) y
los textos con las cifras vivas del estudio. Si se regeneran los parquets,
las reglas se recalibran solas en la siguiente corrida de dormidos.py.
"""
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")


def correr():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    semanas = np.sort(pan.semana.unique())
    u = (pan.pivot(index="codigo", columns="semana", values="unidades_rec")
         .reindex(columns=semanas))
    p = (pan.pivot(index="codigo", columns="semana", values="precio_lista")
         .reindex(columns=semanas).ffill(axis=1))
    mkt = pan.groupby("semana").unidades_rec.sum().reindex(semanas).values
    ex = pd.read_parquet(os.path.join(DATA, "reporte61", "existencias_sem.parquet"))
    col_d = "disp_venta" if "disp_venta" in ex.columns else "disponible"
    disp = (ex.pivot_table(index="codigo", columns="semana", values=col_d,
                           aggfunc="last").reindex(columns=semanas))
    provs = {}
    for ruta in ["proveedores.parquet", "proveedores_inventario.parquet"]:
        rp = os.path.join(DATA, "reporte61", ruta)
        if os.path.exists(rp):
            for c, pr in pd.read_parquet(rp).values:
                provs.setdefault(c, pr)

    comunes = u.index.intersection(disp.index)
    ev_a, ev_b = [], []
    for cod in comunes:
        uv = u.loc[cod].fillna(0.0).values
        pv = p.loc[cod].values
        dv = disp.loc[cod].values
        n = len(uv)
        # ---- A: reabastos ----
        sin = (np.nan_to_num(dv, nan=-1) <= 0)
        i = 8
        while i < n - 6:
            if sin[i] and not sin[i - 1]:          # inicio de stockout
                j = i
                while j < n and sin[j]:
                    j += 1
                dur = j - i
                if dur >= 3 and j < n - 6:         # reabasto en j con 6 sem de futuro
                    base = uv[max(0, i - 8):i].mean()
                    if base >= 1:                   # con vida previa medible
                        p_pre = np.nanmedian(pv[max(0, i - 4):i])
                        p_new = np.nanmedian(pv[j:j + 2])
                        if np.isfinite(p_pre) and p_pre > 0 and np.isfinite(p_new):
                            rel = p_new / p_pre - 1
                            m_pre = np.nanmean(mkt[max(0, i - 8):i])
                            m_post = np.nanmean(mkt[j:j + 6])
                            post = uv[j:j + 6].mean()
                            rec = (post / base) / (m_post / m_pre) if m_pre > 0 and m_post > 0 else np.nan
                            vtas = np.nonzero(uv[j:j + 6] > 0)[0]
                            ev_a.append((cod, provs.get(cod, ""), str(semanas[j])[:10],
                                         dur, round(rel, 4), round(base, 1),
                                         round(rec, 3) if np.isfinite(rec) else np.nan,
                                         int(vtas[0]) + 1 if len(vtas) else np.nan))
                i = j
            else:
                i += 1
        # ---- B: dormidos con stock ----
        con = (np.nan_to_num(dv, nan=0) > 0)
        cero = (uv <= 0)
        i = 8
        while i < n - 16:
            if cero[i] and not cero[i - 1]:        # inicio del silencio
                j = i
                while j < n and cero[j]:
                    j += 1
                dur = j - i
                if dur >= 8 and con[i:i + 8].mean() >= 0.8:   # silencio CON stock
                    base = uv[max(0, i - 8):i].mean()
                    if base < 0.5:
                        i = j
                        continue
                    p0 = np.nanmedian(pv[max(0, i - 4):i])
                    # ¿hubo recorte ≥5% dentro de las primeras 12 sem de silencio?
                    ventana = pv[i:min(i + 12, n)]
                    rel_min = np.nanmin(ventana / p0 - 1) if np.isfinite(p0) and p0 > 0 else np.nan
                    if np.isfinite(rel_min) and rel_min <= -0.05:
                        t_acc = i + int(np.nanargmin(ventana / p0 - 1))
                        accion, mag = "BAJÓ", rel_min
                    else:
                        t_acc = i + 8                       # ancla para el cohorte IGUAL
                        accion, mag = "IGUAL", 0.0
                    if t_acc + 8 <= n:
                        seg = uv[t_acc:t_acc + 8]
                        revivio = (np.count_nonzero(seg > 0) >= 2) or (seg.sum() >= 3)
                        ev_b.append((cod, provs.get(cod, ""), str(semanas[i])[:10],
                                     dur, accion, round(mag, 4), round(base, 1),
                                     bool(revivio), round(float(seg.sum()), 1)))
                i = j
            else:
                i += 1

    A = pd.DataFrame(ev_a, columns=["codigo", "proveedor", "sem_reabasto", "dur_stockout",
                                    "rel_precio", "base_pre", "recuperacion", "sem_1a_venta"])
    B = pd.DataFrame(ev_b, columns=["codigo", "proveedor", "sem_inicio", "dur_silencio",
                                    "accion", "magnitud", "base_pre", "revivio", "u_post8"])
    A.to_parquet(os.path.join(DATA, "analisis_reactivacion_A.parquet"), index=False)
    B.to_parquet(os.path.join(DATA, "analisis_reactivacion_B.parquet"), index=False)

    print(f"== A. REABASTOS (stockout ≥3 sem con vida previa): {len(A):,} eventos, "
          f"{A.codigo.nunique():,} SKUs ==", flush=True)
    A["grupo"] = np.where(A.rel_precio <= -0.03, "BAJÓ ≥3%",
                          np.where(A.rel_precio >= 0.03, "SUBIÓ ≥3%", "IGUAL ±3%"))
    for g, d in A.groupby("grupo"):
        print(f"  {g:<10} n={len(d):>5,} | recuperación mediana {d.recuperacion.median():>5.0%} "
              f"| recupera ≥80% de su venta: {(d.recuperacion >= 0.8).mean():>4.0%} "
              f"| 1a venta en {d.sem_1a_venta.median():.0f} sem", flush=True)

    print(f"\n== B. DORMIDOS CON STOCK (silencio ≥8 sem): {len(B):,} eventos, "
          f"{B.codigo.nunique():,} SKUs ==", flush=True)
    for g, d in B.groupby("accion"):
        print(f"  {g:<6} n={len(d):>5,} | REVIVIÓ: {d.revivio.mean():>4.0%} "
              f"| unidades medianas post-8sem: {d.u_post8.median():.0f}", flush=True)
    bj = B[B.accion == "BAJÓ"].copy()
    if len(bj):
        bj["prof"] = pd.cut(-bj.magnitud, [0.05, 0.10, 0.20, 1.0],
                            labels=["5-10%", "10-20%", ">20%"])
        print("  por PROFUNDIDAD del recorte:", flush=True)
        for g, d in bj.groupby("prof", observed=True):
            print(f"    {g:<7} n={len(d):>4,} | revivió {d.revivio.mean():>4.0%}", flush=True)

    print("\n== B por PROVEEDOR (top por eventos, n≥40) ==", flush=True)
    top = B.groupby("proveedor").filter(lambda d: len(d) >= 40 and d.accion.nunique() == 2)
    filas = []
    for pr, d in top.groupby("proveedor"):
        if not pr:
            continue
        rb = d[d.accion == "BAJÓ"].revivio.mean()
        ri = d[d.accion == "IGUAL"].revivio.mean()
        filas.append((pr[:30], len(d), rb, ri, rb - ri))
    for pr, nn, rb, ri, dd in sorted(filas, key=lambda x: -x[1])[:12]:
        print(f"  {pr:<30} n={nn:>4} | bajó revive {rb:>4.0%} vs igual {ri:>4.0%} "
              f"(Δ {dd:+.0%})", flush=True)
    return A, B


if __name__ == "__main__":
    correr()
