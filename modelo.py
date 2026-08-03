# -*- coding: utf-8 -*-
"""Estima la elasticidad-precio de la demanda desde data/panel.parquet.

Modelo principal: PPML (Poisson con efectos fijos, pyfixest.fepois) — prioridad #1
del v3. La demanda (conteo de unidades) se regresa sobre log(precio de lista) con
FE de SKU (absorbe nivel) y FE de semana (absorbe estacionalidad/tendencia común).
El coeficiente de log(precio) es la elasticidad ε.

Palanca = `precio_lista` (precio administrado 1/3), NO el neto (endógeno).

Se corre también un log1p-OLS como CONTRASTE para exhibir el sesgo que documenta
REVISION_V2 (#1): en demanda de bajo volumen el log1p atenúa |ε| y sesga hacia SUBIR.

Salidas: imprime elasticidades y tendencia; guarda data/elasticidad_sku.parquet
(ε por SKU vía interacción, para el paso de recomendación).

CAVEAT causal: sin instrumento ni existencias, ε es asociacional (precio de lista es
poco endógeno, pero el resultado no es causal puro). Triangulación (IV/DML) = pendiente.
"""
import os

import numpy as np
import pandas as pd
import pyfixest as pf

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PANEL = os.path.join(DATA, "panel.parquet")
OUT = os.path.join(DATA, "elasticidad_sku.parquet")


def cargar():
    pan = pd.read_parquet(PANEL)
    pan = pan[pan.activo].copy()
    # demanda = venta RECURRENTE (sin proyectos): los proyectos son ventas
    # reales pero no indican demanda recurrente (regla del negocio)
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
        print("demanda = unidades RECURRENTES (ventas de proyecto excluidas)", flush=True)
    pan["log_precio"] = np.log(pan.precio_lista.astype(float))
    pan["log_u"] = np.log1p(pan.unidades.astype(float))
    # SKUs con variación de precio dentro de su historia (si no, no identifican ε)
    var = pan.groupby("codigo").log_precio.transform("std").fillna(0)
    pan["con_var"] = var > 1e-4
    print(f"panel modelable: {len(pan):,} celdas | {pan.codigo.nunique():,} SKUs activos | "
          f"{pan.con_var.mean()*100:.0f}% con variación de precio", flush=True)
    return pan


def con_ceros(pan):
    """Rellena con 0 las semanas sin venta DENTRO del span activo de cada SKU.

    Sin esto el PPML solo ve el margen intensivo: un SKU cuyas ventas MURIERON
    tras un aumento desaparece de la estimación justo cuando más informa.
    Ceros solo interiores (entre primera y última venta: el SKU estaba vivo);
    el precio de las semanas cero es el último vigente (ffill, jamás bfill).
    Las semanas posteriores a la última venta NO se imputan (sin inventario no
    se distingue descontinuación de precio alto — hallazgo #2).
    """
    u = pan.pivot(index="codigo", columns="semana", values="unidades")
    p = pan.pivot(index="codigo", columns="semana", values="precio_lista")
    vivo = u.notna().values
    first = vivo.argmax(axis=1)
    last = vivo.shape[1] - 1 - vivo[:, ::-1].argmax(axis=1)
    cols = np.arange(vivo.shape[1])
    interior = (cols >= first[:, None]) & (cols <= last[:, None])
    uf = u.fillna(0.0).where(pd.DataFrame(interior, index=u.index, columns=u.columns))
    pf = p.ffill(axis=1).where(pd.DataFrame(interior, index=u.index, columns=u.columns))
    largo = (uf.stack(future_stack=True).rename("unidades").to_frame()
             .join(pf.stack(future_stack=True).rename("precio_lista")).dropna().reset_index())
    largo["log_precio"] = np.log(largo.precio_lista.astype(float))
    n_ceros = int((largo.unidades == 0).sum())

    # STOCKOUTS (hallazgo #2, ahora con datos): un cero solo informa del precio
    # si HABÍA existencia esa semana. Cero sin stock = no disponible, se excluye.
    ruta_ex = os.path.join(DATA, "reporte61", "existencias_sem.parquet")
    if os.path.exists(ruta_ex):
        ex = pd.read_parquet(ruta_ex)
        ruta_bak = os.path.join(DATA, "reporte61", "existencias_sem_backup.parquet")
        if os.path.exists(ruta_bak):
            bak = pd.read_parquet(ruta_bak)
            if bak.semana.max() > ex.semana.max():
                ex = bak  # re-extracción en curso: usar el snapshot completo
        # stock vendible = disponible en almacenes de VENTA; fallback a columnas viejas
        col_disp = ("disp_venta" if "disp_venta" in ex.columns else
                    ("disponible" if "disponible" in ex.columns else "existencia"))
        largo = largo.merge(ex[["codigo", "semana", col_disp]], on=["codigo", "semana"],
                            how="left")
        stockout = (largo.unidades == 0) & ~(largo[col_disp] > 0)
        largo = largo[~stockout].drop(columns=[col_disp])
        print(f"  panel con ceros interiores: {len(largo):,} celdas (+{n_ceros:,} ceros; "
              f"−{int(stockout.sum()):,} ceros de stockout excluidos)", flush=True)
    else:
        print(f"  panel con ceros interiores: {len(largo):,} celdas (+{n_ceros:,} ceros, "
              f"{100*n_ceros/len(largo):.0f}%) — SIN filtro de stockout (falta "
              f"existencias_sem.parquet)", flush=True)
    return largo


def tendencia(pan):
    """Tendencia de ventas: unidades totales por semana + pendiente log-lineal."""
    wk = pan.groupby("semana").unidades.sum().reset_index().sort_values("semana")
    wk["t"] = np.arange(len(wk))
    b = np.polyfit(wk.t, np.log(wk.unidades), 1)[0]
    print(f"\n== TENDENCIA DE VENTAS ==", flush=True)
    print(f"  semanas: {len(wk)} | unidades/sem: primera {wk.unidades.iloc[0]:,.0f} "
          f"última {wk.unidades.iloc[-1]:,.0f}", flush=True)
    print(f"  tendencia log-lineal: {b*100:+.2f}% por semana (~{b*100*4.345:+.1f}%/mes)", flush=True)


SEGMENTOS = ["bajo", "medio", "alto"]  # terciles de rotación (unidades/sem)
MIN_SKU_PROV = 30  # mínimo de SKUs con variación para estimar ε por proveedor


def elasticidad(pan):
    """PPML global y POR SEGMENTO sobre el panel con ceros interiores.

    El ε global promedia SKUs elásticos e inelásticos ⇒ con |ε|<1 todo sale
    SUBIR (sesgo del promedio). Los segmentos son el primer paso para que las
    recomendaciones BAJAR salgan del modelo donde aplican; con 24 meses el
    siguiente paso es EB por SKU.
    """
    d = pan[pan.con_var]
    z = con_ceros(d)
    print(f"\n== ELASTICIDAD (PPML con ceros interiores; {d.codigo.nunique():,} SKUs) ==",
          flush=True)

    ppml = pf.fepois("unidades ~ log_precio | codigo + semana", data=z, vcov={"CRV1": "codigo"})
    eps_g, se_g = ppml.coef()["log_precio"], ppml.se()["log_precio"]
    print(f"  GLOBAL       ε = {eps_g:+.3f}  (se {se_g:.3f})", flush=True)

    # contraste log1p (solo semanas observadas, como el v2) para dimensionar sesgos
    ols = pf.feols("log_u ~ log_precio | codigo + semana", data=d, vcov={"CRV1": "codigo"})
    print(f"  (contraste log1p sin ceros: ε = {ols.coef()['log_precio']:+.3f})", flush=True)

    # por segmento de rotación (terciles de unidades/sem promedio del SKU)
    u_med = pan.groupby("codigo").unidades.mean()
    terc = pd.qcut(u_med, 3, labels=SEGMENTOS)
    z = z.merge(terc.rename("segmento"), left_on="codigo", right_index=True)
    seg_out = []
    for s in SEGMENTOS:
        ds = z[z.segmento == s]
        try:
            m = pf.fepois("unidades ~ log_precio | codigo + semana", data=ds,
                          vcov={"CRV1": "codigo"})
            e, se = m.coef()["log_precio"], m.se()["log_precio"]
        except Exception as ex:
            print(f"  {s}: no estimable ({str(ex)[:50]}) — usa global", flush=True)
            e, se = eps_g, se_g
        seg_out.append({"segmento": s, "eps": e, "se": se,
                        "n_sku": int(ds.codigo.nunique())})
        print(f"  rotación {s:<6} ε = {e:+.3f}  (se {se:.3f})  [{ds.codigo.nunique():,} SKUs]",
              flush=True)

    # mapa SKU -> ε de su segmento (todos los activos, con o sin variación propia)
    segdf = pd.DataFrame(seg_out).set_index("segmento")
    mapa = terc.rename("segmento").to_frame()
    mapa["eps"] = mapa.segmento.map(segdf.eps).astype(float)
    mapa["se"] = mapa.segmento.map(segdf.se).astype(float)
    mapa["eps_global"], mapa["se_global"] = eps_g, se_g

    # ESCALERA POR PROVEEDOR (regla aprobada 2026-07-27): ε por proveedor con
    # ≥MIN_SKU_PROV SKUs con variación, combinado con el ε del segmento por
    # PRECISIÓN (Empirical Bayes inverso-varianza) — individualiza la curva sin
    # falsa precisión. Sin selección por signo (hallazgo #3a); único guardrail:
    # si el blend queda > −0.1 (≈inelástico perfecto, típico de identificación
    # débil) se regresa al segmento — preferir abstención a opinar mal.
    mapa["nivel"] = "segmento"
    ruta_prov = os.path.join(DATA, "reporte61", "proveedores.parquet")
    if os.path.exists(ruta_prov):
        prov = pd.read_parquet(ruta_prov).set_index("codigo").proveedor
        z["prov"] = z.codigo.map(prov)
        n_var = z[z.unidades.notna()].groupby("prov").codigo.nunique()
        cand = n_var[n_var >= MIN_SKU_PROV].index
        print(f"\n== ESCALERA POR PROVEEDOR ({len(cand)} proveedores con "
              f"≥{MIN_SKU_PROV} SKUs modelables) ==", flush=True)
        prov_out = {}
        for pnombre in cand:
            zp = z[z.prov == pnombre]
            try:
                m = pf.fepois("unidades ~ log_precio | codigo + semana", data=zp,
                              vcov={"CRV1": "codigo"})
                e_p, se_p = float(m.coef()["log_precio"]), float(m.se()["log_precio"])
                if np.isfinite(e_p) and np.isfinite(se_p) and se_p > 0:
                    prov_out[pnombre] = (e_p, se_p)
            except Exception:
                continue
        print(f"  estimados: {len(prov_out)} proveedores", flush=True)
        mapa["prov"] = prov.reindex(mapa.index)
        tiene = mapa.prov.isin(prov_out.keys())
        e_p = mapa.prov.map({k: v[0] for k, v in prov_out.items()})
        se_p = mapa.prov.map({k: v[1] for k, v in prov_out.items()})
        w_p, w_s = 1 / se_p ** 2, 1 / mapa.se ** 2
        e_blend = (w_p * e_p + w_s * mapa.eps) / (w_p + w_s)
        se_blend = np.sqrt(1 / (w_p + w_s))
        ok = tiene & (e_blend <= -0.1)
        mapa.loc[ok, "eps"] = e_blend[ok]
        mapa.loc[ok, "se"] = se_blend[ok]
        mapa.loc[ok, "nivel"] = "proveedor+segmento (EB)"
        mapa = mapa.drop(columns="prov")
        print(f"  SKUs con ε proveedor+segmento: {int(ok.sum()):,} de {len(mapa):,} "
              f"(fallback a segmento: blend >{-0.1} en {int((tiene & ~ok).sum()):,})",
              flush=True)
        print(f"  ε final: mediana {mapa.eps.median():.2f} | p10 {mapa.eps.quantile(.1):.2f} "
              f"| p90 {mapa.eps.quantile(.9):.2f}", flush=True)

    mapa.reset_index(names="codigo").to_parquet(os.path.join(DATA, "eps_por_sku.parquet"),
                                                index=False)
    print(f"  guardado data/eps_por_sku.parquet ({len(mapa):,} SKUs)", flush=True)
    return eps_g, se_g


def main():
    pan = cargar()
    tendencia(pan)
    eps, se = elasticidad(pan)
    # ε global como base; el ε por SKU (EB/interacciones) vendrá en la iteración de recos.
    pd.DataFrame([{"eps_global": eps, "se_global": se,
                   "n_celdas": int(pan.con_var.sum()),
                   "n_sku": int(pan[pan.con_var].codigo.nunique())}]).to_parquet(OUT, index=False)
    print(f"\nguardado {OUT}", flush=True)


if __name__ == "__main__":
    main()
