# -*- coding: utf-8 -*-
"""VENTA CRUZADA: ¿subirle el precio a X arrastra a los modelos que se venden
JUNTOS con X? (usuario 2026-07-31: "analizar folios de factura, etiquetar
movimiento y ver cuando el modelo X subió, qué pasó con el que se vende 10%
combinado").

Diseño en dos pasos, sobre los folios reales (2024→hoy):

  1. PARES DE CANASTA: para cada SKU X con eventos de subida de lista ≥2%
     aislados (los mismos eventos del replay), sus COMPAÑEROS Y = modelos con
     attach alto: P(Y en el folio | X en el folio) ≥ 10%, con ≥30 folios
     conjuntos en los 6 meses previos al evento. Se excluyen folios de
     concepto proyecto (co-ocurrencia por proyecto ≠ complementariedad).

  2. EFECTO CRUZADO: en cada evento de X, medir la venta del compañero Y en
     las 4 semanas post vs 8 pre (ajustada por mercado), EXCLUYENDO a los Y
     cuya propia lista se movió ±2% en la ventana (aislar el efecto cruzado).
     Comparación: ΔY en eventos-de-X vs ΔY del propio Y en semanas sin evento
     (su ruido base). Elasticidad cruzada ≈ mediana(ΔY%) / mediana(ΔX%).

Modos:
  correr()   — estudio par-evento → data/analisis_canasta.parquet
  estricto() — FILTRO DURO del estudio (Y con lista quieta t−8..t+4): las ε
               cruzadas por bucket que consume la regla → _estricto.parquet
  anclas()   — mapa VIGENTE (X, Y, attach) 26 semanas → anclas_canasta.parquet

La regla ⚓ YA ESTÁ ACTIVA en escenarios.py ("hagamos el cambio", 2026-07-31):
bloquea SUBIR cuando el arrastre estimado ≥ 50% de la ganancia propia.
"""
import os
import sys
import glob

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ATTACH_MIN = 0.10
COFOLIOS_MIN = 30


def _folios():
    """Canastas (folio → códigos únicos), sin proyectos ni kits virtuales."""
    partes = []
    rutas = sorted(glob.glob(os.path.join(DATA, "reporte61", "ventas_*.parquet")))
    if not rutas:
        raise SystemExit("analisis_canasta: no hay data/reporte61/ventas_*.parquet — "
                         "corre primero la extracción (extract_api.py)")
    for r in rutas:
        import pyarrow.parquet as pq
        disp = set(pq.read_schema(r).names)
        cols = [c for c in ["fecha", "folio", "codigo", "concepto", "kit"] if c in disp]
        df = pd.read_parquet(r, columns=cols)
        if "kit" in df.columns:
            df = df[df.kit != "Si"]
        df = df[~df.concepto.str.contains("royecto", na=False)]
        partes.append(df[["fecha", "folio", "codigo"]])
    v = pd.concat(partes, ignore_index=True).drop_duplicates(["folio", "codigo"])
    f = pd.to_datetime(v.fecha)
    if f.dt.tz is not None:
        f = f.dt.tz_localize(None)
    v["semana"] = f.dt.to_period("W-SUN").dt.start_time
    return v[["folio", "codigo", "semana"]]


def correr():
    import analisis_eps_sku as aes
    ev = aes.eventos_precio()
    subidas = ev[ev.rel >= 0.02].copy()
    print(f"eventos de subida aislados: {len(subidas):,} en "
          f"{subidas.codigo.nunique():,} SKUs", flush=True)

    v = _folios()
    print(f"canasta: {v.folio.nunique():,} folios | {len(v):,} renglones únicos "
          f"(sin proyectos/kits)", flush=True)
    # índice invertido: codigo → folios; folio → códigos
    v["fol_id"] = pd.factorize(v.folio)[0]
    por_cod = v.groupby("codigo").fol_id.apply(lambda s: np.array(s, dtype=np.int64))
    fol_sem = v.groupby("fol_id").semana.first()
    fol_codes = v.groupby("fol_id").codigo.apply(list)

    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    semanas = np.sort(pan.semana.unique())
    u = (pan.pivot(index="codigo", columns="semana", values="unidades_rec")
         .reindex(columns=semanas))
    p = (pan.pivot(index="codigo", columns="semana", values="precio_lista")
         .reindex(columns=semanas).ffill(axis=1))
    mkt = pan.groupby("semana").unidades_rec.sum().reindex(semanas).values
    sem_idx = {pd.Timestamp(s): k for k, s in enumerate(semanas)}

    pares = []
    for _, e in subidas.iterrows():
        X = e.codigo
        if X not in por_cod.index:
            continue
        t0 = sem_idx.get(pd.Timestamp(e.semana))
        if t0 is None or t0 < 8 or t0 > len(semanas) - 5:
            continue
        # attach en los 6 meses PREVIOS al evento
        ventana_ini = semanas[max(0, t0 - 26)]
        fols_X = por_cod[X]
        fols_X = fols_X[(fol_sem.reindex(fols_X).values >= ventana_ini)
                        & (fol_sem.reindex(fols_X).values < semanas[t0])]
        if len(fols_X) < COFOLIOS_MIN:
            continue
        comp = pd.Series(np.concatenate([fol_codes[f] for f in fols_X])).value_counts()
        comp = comp.drop(X, errors="ignore")
        comp = comp[comp >= max(COFOLIOS_MIN, ATTACH_MIN * len(fols_X))]
        for Y, n_co in comp.items():
            if Y not in u.index or Y not in p.index:
                continue
            pvY = p.loc[Y].values
            pY0 = np.nanmedian(pvY[max(0, t0 - 4):t0])
            pY1 = np.nanmedian(pvY[t0:t0 + 4])
            if not (np.isfinite(pY0) and pY0 > 0 and np.isfinite(pY1)):
                continue
            if abs(pY1 / pY0 - 1) >= 0.02:      # Y movió su propia lista: fuera
                continue
            uvY = u.loc[Y].fillna(0.0).values
            base = uvY[t0 - 8:t0].mean()
            if base < 1:
                continue
            m_pre = np.nanmean(mkt[t0 - 8:t0])
            m_post = np.nanmean(mkt[t0:t0 + 4])
            if not (m_pre > 0 and m_post > 0):
                continue
            dY = (uvY[t0:t0 + 4].mean() / base) / (m_post / m_pre) - 1
            pares.append((X, Y, str(e.semana)[:10], round(e.rel, 4),
                          round(n_co / len(fols_X), 3), int(n_co),
                          round(dY, 4), round(base, 1)))
    C = pd.DataFrame(pares, columns=["X", "Y", "semana", "dX_precio", "attach",
                                     "co_folios", "dY_venta", "baseY"])
    C.to_parquet(os.path.join(DATA, "analisis_canasta.parquet"), index=False)

    print(f"\n== EFECTO CRUZADO: {len(C):,} pares-evento "
          f"({C.X.nunique():,} anclas X, {C.Y.nunique():,} compañeros Y) ==", flush=True)
    if C.empty:
        return C
    print(f"  ΔX precio mediano: {100*C.dX_precio.median():+.1f}% | "
          f"ΔY venta mediana (ajustada mercado): {100*C.dY_venta.median():+.1f}%", flush=True)
    ec = C.dY_venta.median() / C.dX_precio.median()
    print(f"  elasticidad CRUZADA mediana ≈ {ec:+.2f} "
          f"(negativa = complementos reales)", flush=True)
    C["fuerza"] = pd.cut(C.attach, [0.10, 0.20, 0.40, 1.0],
                         labels=["attach 10-20%", "20-40%", ">40%"])
    for g, d in C.groupby("fuerza", observed=True):
        print(f"  {g:<14} n={len(d):>5,} | ΔY mediana {100*d.dY_venta.median():+.1f}% | "
              f"ε cruzada ≈ {d.dY_venta.median()/d.dX_precio.median():+.2f}", flush=True)
    # control de ruido: ΔY típico del catálogo sin evento (placebo grueso)
    print(f"  (dX mediana de referencia {100*C.dX_precio.median():.1f}%; pares con "
          f"ΔY < −10%: {(C.dY_venta < -0.10).mean():.0%} vs ΔY > +10%: "
          f"{(C.dY_venta > 0.10).mean():.0%})", flush=True)
    return C


def estricto():
    """FILTRO DURO (ronda 3: persistir el script del estudio endurecido que
    produjo las ε de la regla — antes vivía solo en un ad-hoc). Igual que
    correr() pero el compañero Y debe tener la lista QUIETA en TODA la ventana
    t−8..t+4 (±2%): elimina la coartada "el principal subió y eso arrastró".
    Salida: data/analisis_canasta_estricto.parquet + ε por bucket (las que
    consume escenarios.py: attach 10-20 / 20-40 / >40)."""
    import analisis_eps_sku as aes
    ev = aes.eventos_precio()
    subidas = ev[ev.rel >= 0.02].copy()
    v = _folios()
    v["fol_id"] = pd.factorize(v.folio)[0]
    por_cod = v.groupby("codigo").fol_id.apply(lambda s: np.array(s, dtype=np.int64))
    fol_sem = v.groupby("fol_id").semana.first()
    fol_codes = v.groupby("fol_id").codigo.apply(list)
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    semanas = np.sort(pan.semana.unique())
    u = (pan.pivot(index="codigo", columns="semana", values="unidades_rec")
         .reindex(columns=semanas))
    p = (pan.pivot(index="codigo", columns="semana", values="precio_lista")
         .reindex(columns=semanas).ffill(axis=1))
    mkt = pan.groupby("semana").unidades_rec.sum().reindex(semanas).values
    sem_idx = {pd.Timestamp(s): k for k, s in enumerate(semanas)}
    pares = []
    for _, e in subidas.iterrows():
        X = e.codigo
        if X not in por_cod.index:
            continue
        t0 = sem_idx.get(pd.Timestamp(e.semana))
        if t0 is None or t0 < 8 or t0 > len(semanas) - 5:
            continue
        ventana_ini = semanas[max(0, t0 - 26)]
        fols_X = por_cod[X]
        fols_X = fols_X[(fol_sem.reindex(fols_X).values >= ventana_ini)
                        & (fol_sem.reindex(fols_X).values < semanas[t0])]
        if len(fols_X) < COFOLIOS_MIN:
            continue
        comp = pd.Series(np.concatenate([fol_codes[f] for f in fols_X])).value_counts()
        comp = comp.drop(X, errors="ignore")
        comp = comp[comp >= max(COFOLIOS_MIN, ATTACH_MIN * len(fols_X))]
        for Y, n_co in comp.items():
            if Y not in u.index or Y not in p.index:
                continue
            # FILTRO DURO: lista de Y quieta en TODA la ventana t−8..t+4
            pvY = p.loc[Y].values[t0 - 8:t0 + 4]
            pY0 = np.nanmedian(pvY[:8])
            if not (np.isfinite(pY0) and pY0 > 0) or np.isnan(pvY).all():
                continue
            if np.nanmax(np.abs(pvY / pY0 - 1)) >= 0.02:
                continue
            uvY = u.loc[Y].fillna(0.0).values
            base = uvY[t0 - 8:t0].mean()
            if base < 1:
                continue
            m_pre = np.nanmean(mkt[t0 - 8:t0])
            m_post = np.nanmean(mkt[t0:t0 + 4])
            if not (m_pre > 0 and m_post > 0):
                continue
            dY = (uvY[t0:t0 + 4].mean() / base) / (m_post / m_pre) - 1
            pares.append((X, Y, str(e.semana)[:10], round(e.rel, 4),
                          round(n_co / len(fols_X), 3), int(n_co),
                          round(dY, 4), round(base, 1)))
    C = pd.DataFrame(pares, columns=["X", "Y", "semana", "dX_precio", "attach",
                                     "co_folios", "dY_venta", "baseY"])
    C.to_parquet(os.path.join(DATA, "analisis_canasta_estricto.parquet"), index=False)
    print(f"\n== FILTRO DURO: {len(C):,} pares-evento ==", flush=True)
    if len(C):
        print(f"  ε cruzada mediana ≈ {C.dY_venta.median()/C.dX_precio.median():+.2f}",
              flush=True)
        C["fuerza"] = pd.cut(C.attach, [0.10, 0.20, 0.40, 1.0],
                             labels=["attach 10-20%", "20-40%", ">40%"])
        for g, d in C.groupby("fuerza", observed=True):
            print(f"  {g:<14} n={len(d):>5,} | ε ≈ "
                  f"{d.dY_venta.median()/d.dX_precio.median():+.2f}", flush=True)
        print("  (si estas ε divergen de las de escenarios.py — 0.80/0.78/2.27 — "
              "actualizar la regla)", flush=True)
    return C


def anclas():
    """MAPA DE ANCLAS para la regla aprobada (usuario 2026-07-31, "hagamos el
    cambio"): pares (X ancla, Y compañero) con attach VIGENTE — folios de los
    últimos 6 meses, sin proyectos/kits, attach ≥10% y ≥30 co-folios.
    escenarios.py lo cruza con las ε cruzadas por bucket (filtro duro) para el
    margen arrastrado de canasta. Regenerar con cada corrida (run.py)."""
    v = _folios()
    corte = v.semana.max()
    v = v[v.semana >= corte - pd.Timedelta(weeks=26)]
    v["fol_id"] = pd.factorize(v.folio)[0]
    por_cod = v.groupby("codigo").fol_id.apply(lambda s: np.array(s, dtype=np.int64))
    fol_codes = v.groupby("fol_id").codigo.apply(list)
    pares = []
    for X, fols in por_cod.items():
        if len(fols) < COFOLIOS_MIN:
            continue
        comp = pd.Series(np.concatenate([fol_codes[f] for f in fols])).value_counts()
        comp = comp.drop(X, errors="ignore")
        comp = comp[comp >= max(COFOLIOS_MIN, ATTACH_MIN * len(fols))]
        for Y, n_co in comp.items():
            pares.append((X, Y, round(n_co / len(fols), 3), int(n_co)))
    A = pd.DataFrame(pares, columns=["X", "Y", "attach", "co_folios"])
    # sello de corte = corte del PANEL (escenarios avisa si el mapa envejece);
    # el corte de folios puede ir 1 semana adelante (semana parcial en curso)
    ruta_pan = os.path.join(DATA, "panel.parquet")
    if os.path.exists(ruta_pan):
        import pyarrow.parquet as pq
        A["corte"] = pd.Timestamp(pq.read_table(ruta_pan, columns=["semana"])
                                  .column("semana").to_pandas().max())
    else:
        A["corte"] = pd.Timestamp(corte)
    ruta = os.path.join(DATA, "anclas_canasta.parquet")
    A.to_parquet(ruta, index=False)
    print(f"mapa de anclas: {A.X.nunique():,} anclas × {len(A):,} pares "
          f"(folios de 26 semanas al {pd.Timestamp(corte).date()}) → {ruta}", flush=True)
    return A


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    {"anclas": anclas, "estricto": estricto}.get(modo, correr)()
