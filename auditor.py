"""AUDITOR DE COSTOS — la sección de restauración de factor (usuario 2026-08-14).

Distinta del motor por diseño: el motor decide el NIVEL del precio (elasticidad,
competencia, cadencia de 3 semanas, paso ±4pts). Este auditor decide la
RECUPERACIÓN DE COSTO — restaurar el factor de la política de SYSCOM cuando el
costo del proveedor se mueve. Son dos deltas distintos y no se mezclan:

  · factor       = precio_vigente / costo_base      (política, por proveedor)
  · precio_pol   = costo_nuevo × factor_antes       (restaurar = mover el precio
                                                     el MISMO % que el costo)

Por eso está exento del paso de ±4pts: es defensa de estructura, no optimización
(87% de las alzas de costo medidas requieren más de 4%).

Lo que agrega sobre "notificar que el costo cambió": el PAYLOAD que permite
juzgar si el traslado va al 100% o no. Medido sobre los 432 pendientes del
2026-08-14, con su cobertura real:

  ε × alza  (daño de volumen esperado)   88.7%  ← el número central
  mezcla de canal (pct_linea)            96.8%  alzas aterrizan mejor en línea
  n_clientes  (≤2 ⇒ es negociación)      96.8%  37.5% de los eventos
  crecimiento + bandera de stockout      60.6%  "baja venta" ≠ demanda cayendo
  historia propia del modelo             54.0%  ya estaba calculada, sin usar
  es ancla de canasta                    36.1%  sólo si el arrastre es material
  sobrestock / holdout / remate          16.0% / 8.6% / 0.9%
  semáforo competitivo                   17.8%  decisivo donde existe

El veredicto NO es un score: es APLICAR / FRENO / CONFIRMAR COSTO con razón
nombrada, y NO existe un traslado "a medias". Dos reglas del negocio
(usuario 2026-08-14) que el código respeta:

  · El precio del competidor es REFERENCIA, no dato duro: se muestra en la
    fila, nunca frena un aumento.
  · Es preferible que el cliente reclame que estamos caros a dejar el precio
    abajo, así que no hay traslado parcial "por pocos clientes".

Ambas coinciden con nuestra propia medición: el tramo de traslado 0-50% fue el
PEOR resultado (-7.1% de venta Y -2.6 pts de margen). Trasladar a medias es la
peor de las opciones, así que el traslado es completo o no es.

Salida: out/auditor_costos.parquet (lo lee reporte_top.py para la vista).
Uso:    ./.venv/bin/python auditor.py
"""
import os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
VIGENCIA_D = 60          # ventana de 2 meses (usuario 2026-08-14)
UMBRAL_PCT = 2.0         # mismo umbral de la vigía: |Δcosto| ≥2% dispara revisión

# DOS FUENTES DE COSTO, deliberadamente separadas y etiquetadas:
#
#  · "reposición" — vigía diaria (out/revision_costos.csv), costo_proveedor de
#    reportes.revision_precios. Es el TITULAR: es el costo de reponer, el que
#    la política usa para re-armar el precio. Preciso y diario, PERO la vigía
#    arrancó el 2026-07-28, así que sólo cubre desde entonces.
#  · "COGS" — panel semanal (costo_prom), el costo que de verdad pagamos en las
#    ventas de la semana. Cubre 2024→hoy, así que llena la ventana de 2 meses.
#    Señal más débil para la política: un movimiento de COGS puede venir de la
#    mezcla de lotes o de una compra puntual, no necesariamente de un cambio de
#    lista del proveedor. Se marca y el veredicto lo dice.
#
# NUNCA se mezclan en un mismo número: confundir COGS con costo de reposición es
# exactamente el defecto que hace que 170 modelos aparezcan con margen positivo
# mientras venden por debajo de lo que cuesta reponerlos.


def _eventos_panel(dias):
    """Eventos de costo reconstruidos del panel (costo_prom semanal), para
    cubrir la parte de la ventana anterior al arranque de la vigía.

    Acumula igual que la vigía: primer costo observado de la ventana como base
    contra el último, no cambio semana a semana (si no, un ida-y-vuelta contaría
    como dos eventos y el pct se descontaría dos veces)."""
    fp = os.path.join(BASE, "data", "panel.parquet")
    if not os.path.exists(fp):
        return pd.DataFrame()
    pan = pd.read_parquet(fp, columns=["codigo", "semana", "costo_prom", "unidades"])
    pan = pan[(pan.unidades > 0) & (pan.costo_prom > 0)]      # el costo sólo se observa al vender
    corte = pan.semana.max() - pd.Timedelta(days=dias)
    pan = pan[pan.semana >= corte].sort_values("semana")
    if pan.empty:
        return pd.DataFrame()
    g = pan.groupby("codigo")
    e = pd.DataFrame({
        "costo_base": g.costo_prom.first(), "costo_hoy": g.costo_prom.last(),
        "fecha_ultimo": g.semana.last(), "n_sem": g.size(),
    })
    e = e[e.n_sem >= 2]                                       # necesita dos puntos para un delta
    e["pct"] = (100 * (e.costo_hoy / e.costo_base - 1)).round(1)
    e = e[e.pct.abs() >= UMBRAL_PCT].copy()
    # FECHA DEL SALTO, no la primera semana de la ventana: la primera semana en
    # que el costo ya se había separado ≥2% de su base. Sin esto, el reloj de
    # "días sin reaccionar" diría ~60 días para todos, que es la ventana, no el
    # tiempo que el costo lleva movido.
    p = pan[pan.codigo.isin(e.index)].copy()
    p["base"] = p.codigo.map(e.costo_base)
    salto = (p[(p.costo_prom / p.base - 1).abs() >= UMBRAL_PCT / 100]
             .groupby("codigo").semana.first())
    e["fecha_detectado"] = pd.Series(e.index.map(salto), index=e.index).fillna(e.fecha_ultimo)
    e["fuente_costo"] = "COGS"
    return e.reset_index()


def _snapshot():
    """Último snapshot de la vigía: costo de reposición + precios vigentes."""
    d = os.path.join(BASE, "data", "vigia")
    fs = sorted(f for f in os.listdir(d) if f.startswith("snap_")) if os.path.isdir(d) else []
    if not fs:
        return None
    s = pd.read_parquet(os.path.join(d, fs[-1]))
    for c in ("precio_1", "precio_3", "costo_prov", "existencia"):
        if c in s.columns:
            s[c] = pd.to_numeric(s[c], errors="coerce")
    # precio vigente = el aplicable: oferta (3) si existe, si no lista (1)
    s["precio_vig"] = np.where(s.precio_3.fillna(0) > 0, s.precio_3, s.precio_1)
    return s.set_index("codigo")


def _stock_restringido(codigos):
    """Bandera: ¿el modelo pasó semanas SIN stock suficiente para su demanda típica?

    Sin esto, `crecimiento` engaña: una venta cayendo por falta de inventario se
    lee como demanda cayendo, y subir el precio 'porque ya vende poco' sería
    exactamente el error inverso (caso ACCESSTAGV2, 2026-08-11). Criterio de
    COBERTURA, no de cero absoluto: el modelo nunca llegó a cero, tenía 321-825
    unidades para una demanda de ~10K/semana.
    """
    fe = os.path.join(BASE, "data", "reporte61", "existencias_sem.parquet")
    fp = os.path.join(BASE, "data", "panel.parquet")
    if not (os.path.exists(fe) and os.path.exists(fp)):
        return pd.Series(dtype=float)
    ex = pd.read_parquet(fe)
    col = next((c for c in ("disp_venta", "disponible", "existencia") if c in ex.columns), None)
    if col is None or "semana" not in ex.columns:
        return pd.Series(dtype=float)
    pan = pd.read_parquet(fp, columns=["codigo", "semana", "unidades"])
    corte = pan.semana.max() - pd.Timedelta(weeks=26)
    typ = (pan[(pan.unidades > 0) & (pan.semana >= corte)]
           .groupby("codigo").unidades.median())          # demanda típica semanal
    ex = ex[ex.codigo.isin(codigos) & (pd.to_datetime(ex.semana) >= corte)].copy()
    ex["typ"] = ex.codigo.map(typ)
    ex["restr"] = ((pd.to_numeric(ex[col], errors="coerce").fillna(0) < ex.typ)
                   & ex.typ.notna()).astype(float)
    return ex.groupby("codigo").restr.mean()              # fracción de semanas restringidas


def _historia(codigos):
    """Su PROPIA historia: la última vez que a este modelo le movieron el costo,
    ¿cuánto trasladamos y qué le pasó a la venta?

    Es el dato más persuasivo del payload y ya estaba calculado en
    analisis_costos.parquet sin que nadie lo viera. La revisión RBB/OFT lo
    recomienda explícitamente por encima de suponer una forma de demanda:
    "las predicciones basadas en el traslado histórico observado funcionan
    mejor que las simulaciones que asumen una forma de demanda".
    """
    f = os.path.join(BASE, "data", "analisis_costos.parquet")
    if not os.path.exists(f):
        return pd.DataFrame()
    ac = pd.read_parquet(f)
    ac = ac[ac.codigo.isin(codigos) & (ac.u_pre >= 3)]    # con venta previa medible
    if ac.empty:
        return pd.DataFrame()
    ac = ac.sort_values("semana").groupby("codigo").tail(1).set_index("codigo")
    return ac[["semana", "d_costo_pct", "pass_through", "d_venta_pct",
               "d_margen_pts", "lag_sem"]]


def construir():
    rc_p = os.path.join(BASE, "out", "revision_costos.csv")
    rec_p = os.path.join(BASE, "out", "recomendaciones.csv")
    if not (os.path.exists(rc_p) and os.path.exists(rec_p)):
        print("faltan out/revision_costos.csv o out/recomendaciones.csv")
        return pd.DataFrame()
    rc = pd.read_csv(rc_p)
    rc["codigo"] = rc.codigo.astype(str)
    rc["fecha_detectado"] = pd.to_datetime(rc.fecha_detectado)
    rc["fecha_ultimo"] = pd.to_datetime(rc.fecha_ultimo)
    rc = rc[rc.fecha_ultimo >=
            pd.Timestamp.today().normalize() - pd.Timedelta(days=VIGENCIA_D)].copy()
    rc["fuente_costo"] = "reposición"
    print(f"ventana de {VIGENCIA_D} días:")
    print(f"  vigía (costo de reposición): {len(rc)} eventos"
          + (f" · {rc.fecha_detectado.min().date()} → {rc.fecha_ultimo.max().date()}" if len(rc) else ""))

    # el panel llena la parte de la ventana anterior al arranque de la vigía
    ep = _eventos_panel(VIGENCIA_D)
    if len(ep):
        nuevos = ep[~ep.codigo.isin(set(rc.codigo))]          # la vigía es TITULAR donde ambas ven
        print(f"  panel (COGS pagado):        {len(ep)} eventos · "
              f"{ep.fecha_detectado.min().date()} → {ep.fecha_ultimo.max().date()} "
              f"({len(ep) - len(nuevos)} ya vistos por la vigía, se descartan)")
        rc = pd.concat([rc, nuevos], ignore_index=True)
    print(f"  TOTAL a auditar: {len(rc)} eventos sobre {rc.codigo.nunique()} modelos")
    if rc.empty:
        return pd.DataFrame()

    rec = pd.read_csv(rec_p).set_index("codigo")
    snap = _snapshot()
    A = rc.set_index("codigo").join(
        rec[["proveedor", "direccion", "eps", "confianza", "crecimiento", "pct_linea",
             "n_clientes", "meses_inv", "holdout", "remate", "clasif_erp",
             "u_sem_actual", "utilidad_sem_mantener", "margen_actual", "ancla_n",
             "ancla_arrastre", "rol", "segmento"]], how="left")
    if snap is not None:
        A = A.join(snap[["precio_vig", "costo_prov", "existencia"]], how="left")
        if "tipo_prov" in snap.columns:                       # el panel no lo trae
            desde_snap = pd.Series(A.index.map(snap.tipo_prov), index=A.index)
            A["tipo_prov"] = (A.tipo_prov.fillna(desde_snap) if "tipo_prov" in A.columns
                              else desde_snap)
    # los eventos del panel no traen margen: se completan con la misma fórmula
    # de la vigía (mismo neto, costo nuevo) para que la columna sea comparable
    m0 = pd.to_numeric(A.margen_actual, errors="coerce")
    if "margen_antes" not in A.columns:
        A["margen_antes"], A["margen_nuevo"] = np.nan, np.nan
    falta = A.margen_antes.isna()
    A.loc[falta, "margen_antes"] = m0[falta]
    A.loc[falta, "margen_nuevo"] = (1 - (A.costo_hoy[falta] / A.costo_base[falta])
                                    * (1 - m0[falta])).round(4)

    # ---- FACTOR y PRECIO POR POLÍTICA -------------------------------------
    # El factor de la política es sobre el precio APLICABLE (oferta 3 si existe,
    # si no lista 1) contra el costo. OJO: `margen_antes/nuevo` del registro
    # vienen de `margen_actual`, que es margen sobre el NETO REALIZADO (después
    # de descuentos), NO sobre la lista — sirven para mostrar la compresión de
    # margen, jamás para derivar el factor (el neto realiza sólo 60% de la lista).
    A["factor_antes"] = A.precio_vig / A.costo_base.replace(0, np.nan)
    A["factor_hoy"] = A.precio_vig / A.costo_hoy.replace(0, np.nan)
    # restaurar el factor ⇒ el precio se mueve el MISMO % que el costo
    A["precio_pol"] = A.costo_hoy * A.factor_antes
    A["alza_precio_pct"] = 100.0 * (A.precio_pol / A.precio_vig.replace(0, np.nan) - 1.0)
    # ¿el precio YA reaccionó? Se compara contra el snapshot del día en que se
    # DETECTÓ el costo (la vigía guarda uno diario) — no contra un precio
    # inferido, que fue el error de la primera versión. Da además los días
    # transcurridos: el KPI de days-to-react que la literatura no publica.
    hoy = pd.Timestamp.today().normalize()
    # los DÍAS salen de la fecha, no del snapshot: aplican a todos los eventos
    # aunque no exista foto de ese día (los del panel caen en semanas sin snapshot)
    A["dias"] = (hoy - pd.to_datetime(A.fecha_detectado)).dt.days
    A["precio_det"] = np.nan
    dv = os.path.join(BASE, "data", "vigia")
    if os.path.isdir(dv):
        snaps = {f[5:15]: f for f in os.listdir(dv) if f.startswith("snap_")}
        for fd in A.fecha_detectado.dropna().unique():
            f = snaps.get(str(fd)[:10])
            if not f:
                continue
            s0 = pd.read_parquet(os.path.join(dv, f))
            p3 = pd.to_numeric(s0.precio_3, errors="coerce").fillna(0)
            s0["pv"] = np.where(p3 > 0, p3, pd.to_numeric(s0.precio_1, errors="coerce"))
            m = A.fecha_detectado == fd
            A.loc[m, "precio_det"] = A.index[m].map(s0.set_index("codigo").pv)
    A["ya_reacciono"] = ((A.precio_vig - A.precio_det).abs()
                         / A.precio_det.replace(0, np.nan) > 0.01)

    # ---- PAYLOAD DE DECISIÓN ---------------------------------------------
    eps = pd.to_numeric(A.eps, errors="coerce")
    A["dano_pct"] = eps * A.alza_precio_pct            # ε × alza = daño de unidades
    restr = _stock_restringido(set(A.index))
    A["stock_restr"] = A.index.map(restr).astype(float)
    A["crec_sospechoso"] = A.stock_restr.fillna(0) >= 0.30   # el crecimiento no es de demanda
    hist = _historia(set(A.index))
    for c in ("d_costo_pct", "pass_through", "d_venta_pct", "d_margen_pts"):
        A["h_" + c] = A.index.map(hist[c]) if c in hist.columns else np.nan
    sm_p = os.path.join(BASE, "data", "competencia", "semaforo_modelo.parquet")
    if os.path.exists(sm_p):
        sm = pd.read_parquet(sm_p).set_index("codigo")
        A["comp_gap"] = A.index.map(sm.gap_pct)
        A["comp_sem"] = A.index.map(sm.semaforo)
    else:
        A["comp_gap"], A["comp_sem"] = np.nan, ""

    # ---- VEREDICTO: APLICAR / FRENO / CONFIRMAR COSTO / BAJA, con razón --
    mi = pd.to_numeric(A.meses_inv, errors="coerce")
    nc = pd.to_numeric(A.n_clientes, errors="coerce")
    en_remate = (A.remate.astype(str).str.lower().isin(["true", "1", "si", "sí"])
                 | A.clasif_erp.astype(str).str.upper().str.startswith(
                     ("R0", "R1", "R2", "R3", "R4")))
    en_holdout = A.holdout.astype(str).str.lower().isin(["true", "1"])
    # La razón viaja al reporte como CÓDIGO, no como texto: los 2,150 eventos
    # generaban 0.30 MB de prosa repetida y el HTML rebasaba el límite de 16 MB
    # de publicación. El texto se arma en JS con los números que ya van en la
    # fila (factor, alza, daño, meses de stock, clientes).
    ver, raz, cod_raz = [], [], []
    def _add(v, c, t):
        ver.append(v); cod_raz.append(c); raz.append(t)
    for cod, x in A.iterrows():
        if pd.isna(x.alza_precio_pct):
            _add("SIN DATO", "sd", "falta el precio vigente: el modelo no está en la lista que vigila la vigía"); continue
        if x.pct < 0:
            _add("BAJA", "bj", "el costo BAJÓ: la política implica bajar el precio. La asimetría legítima "
                 "es de velocidad, no de destino — confirmar 2 ciclos antes de aplicar"); continue
        if bool(x.get("ya_reacciono", False)):
            _add("YA APLICADO", "ya", "el precio ya se movió desde que se detectó el costo"); continue
        if en_remate.get(cod, False):
            _add("FRENO", "rm", "en remate/salida: no sube (la clasificación del ERP manda)"); continue
        if pd.notna(mi.get(cod)) and mi.get(cod) >= 12:
            _add("FRENO", "so", f"sobrestock {mi.get(cod):.0f} meses: subir agrava el inventario "
                 "(con margen, la decisión correcta es bajar para rotar)"); continue
        if en_holdout.get(cod, False):
            _add("FRENO", "ho", "reservado en holdout para medir: no se toca"); continue
        # NO se frena por competencia (usuario 2026-08-14): el precio del rival
        # es REFERENCIA, no dato duro — se muestra en la fila, nunca decide.
        # Tampoco existe un traslado parcial "por pocos clientes": la postura del
        # negocio es que es preferible que el cliente reclame que estamos caros
        # a dejar el precio abajo. Coincide con nuestra propia medición: el tramo
        # de traslado 0-50% fue el peor resultado (-7.1% de venta Y -2.6 pts).
        d = f"daño esperado {x.dano_pct:.0f}% de unidades" if pd.notna(x.dano_pct) else "sin ε confiable"
        if str(x.get("fuente_costo", "")) == "COGS":
            # el movimiento viene del costo PAGADO (mezcla de lotes o compra
            # puntual), no de un cambio de lista confirmado del proveedor
            _add("CONFIRMAR COSTO", "cg", f"movimiento del costo PAGADO (COGS), no confirmado como cambio de "
                 f"lista del proveedor: restaurar el factor {x.factor_antes:.2f} daría "
                 f"{x.alza_precio_pct:+.1f}% ({d}) — confirmar el costo antes de aplicar")
            continue
        _add("APLICAR", "sf", f"sin freno: restaurar factor {x.factor_antes:.2f} ⇒ "
             f"{x.alza_precio_pct:+.1f}% de precio ({d})")
    A["veredicto"], A["razon"], A["razon_cod"] = ver, raz, cod_raz
    return A.reset_index()


if __name__ == "__main__":
    A = construir()
    if len(A):
        print(f"\nveredictos: " + " | ".join(f"{k}: {v}" for k, v in A.veredicto.value_counts().items()))
        ap = A[A.veredicto == "APLICAR"]
        print(f"APLICAR: {len(ap)} modelos | alza de precio mediana {ap.alza_precio_pct.median():+.1f}% | "
              f"${ap.utilidad_sem_mantener.sum():,.0f}/sem protegidos")
        print(f"  cuántos exceden el paso de ±4pts del motor: {(ap.alza_precio_pct.abs() > 4).sum()} "
              f"({(ap.alza_precio_pct.abs() > 4).mean():.0%}) — por eso es una sección aparte")
        d = pd.to_numeric(A.dias, errors="coerce")
        print(f"\nreloj de reacción (días desde que se movió el costo):")
        print(f"  mediana {d.median():.0f}d | p90 {d.quantile(.9):.0f}d | máx {d.max():.0f}d")
        for lo, hi, lab in [(0, 6, "menos de 1 semana"), (7, 20, "1 a 3 semanas"),
                            (21, 10**6, "más de 3 semanas (un ciclo completo)")]:
            m = (d >= lo) & (d <= hi)
            pend = m & (A.veredicto == "APLICAR")
            print(f"  {lab:<38} {int(m.sum()):>4} eventos | {int(pend.sum()):>3} aún pendientes de aplicar")
        ya = A[A.veredicto == "YA APLICADO"]
        if len(ya):
            print(f"  de los {len(ya)} que YA se movieron: mediana {pd.to_numeric(ya.dias).median():.0f} días en reaccionar")
        print(f"\ncobertura del payload:")
        for lab, m in [("ε confiable (alta/media)", A.confianza.astype(str).isin(["alta", "media"])),
                       ("historia propia del modelo", A.h_pass_through.notna()),
                       ("competencia (sólo referencia)", A.comp_gap.notna()),
                       ("crecimiento sospechoso por stock", A.crec_sospechoso),
                       ("1-2 clientes (negociación)", pd.to_numeric(A.n_clientes, errors="coerce") <= 2)]:
            print(f"  {lab:<36} {int(m.sum()):>4} / {len(A)}  {m.mean():>5.1%}")
        top = A[A.veredicto == "APLICAR"].nlargest(5, "utilidad_sem_mantener")
        print("\ntop 5 por utilidad en juego:")
        for _, x in top.iterrows():
            print(f"  {x.codigo[:24]:<24} costo {x.pct:+.1f}% ⇒ precio {x.alza_precio_pct:+.1f}% | "
                  f"factor {x.factor_antes:.2f} | ${x.utilidad_sem_mantener:,.0f}/sem")
        sal = os.path.join(BASE, "out", "auditor_costos.parquet")
        A.to_parquet(sal, index=False)
        print(f"\n→ {sal}")
