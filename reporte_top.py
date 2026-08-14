# -*- coding: utf-8 -*-
"""Genera out/reporte_precios.html con TRES vistas:
  1. Motor (todos los evaluables): filtros por dirección, rol KVI/SD/PG,
     proveedor, confianza, costo-movió y búsqueda por código
  2. Dormidos (2ª capa): ≥8 semanas de muerte efectiva CON stock vendible
  3. Simulador: la misma matemática del motor por modelo (u=u0·f^ε)

Arquitectura: las filas viajan como JSON compacto y se renderizan con JS al
aplicar filtros (máx 500 visibles con contador); los paneles individuales van
embebidos solo para los top-N por dirección (clic en esas filas los despliega).
Para cualquier otro SKU: ./.venv/bin/python panel_sku.py <CODIGO>.

Uso:  ./.venv/bin/python reporte_top.py [N_paneles_por_direccion] [N_top_filas]
      (defaults: 30 paneles, 200 filas por dirección)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

from panel_sku import (BASE, ESTILOS, JS_LIB, PIE, _safe, _usd,
                       cargar_ctx, cuerpo_dormido, cuerpo_sku)

OUT = os.path.join(BASE, "out", "reporte_precios.html")


def _cargar_calibracion():
    ruta = os.path.join(BASE, "data", "calibracion_decisiones.parquet")
    return pd.read_parquet(ruta) if os.path.exists(ruta) else None


def _clasif_remate(x):
    """Nivel para el chip 🏷️ REMATE: clasificación R0-R4 o 'S' si el flag viene
    sin nivel. NaN-safe (ronda 3, H14: str(nan)='nan' es truthy y pintaba el
    chip como 'REMATE nan')."""
    if not bool(getattr(x, "remate", False)):
        return ""
    c = getattr(x, "clasif_erp", "")
    c = "" if pd.isna(c) else str(c).strip()
    return c if c and c.lower() != "nan" else "S"


def generar(n_paneles=30, n_top=200):
    ctx = cargar_ctx()
    calib = _cargar_calibracion()
    frenos = ctx.get("frenos")
    recos, pan = ctx["recos"], ctx["pan"]
    semanas = np.sort(pan.semana.unique())
    corte = pd.Timestamp(semanas[-1]).date()
    eps_g = float(ctx["eps_g"].eps_global)

    # ---- KPIs globales (todo el evaluable) ----
    rev_ser = recos.revisar.fillna("")
    n_subir = int((recos.direccion == "SUBIR").sum())
    n_bajar = int((recos.direccion == "BAJAR").sum())
    n_abs = int((recos.confianza == "baja").sum())
    ok = recos.confianza != "baja"
    d_tot = float((recos[ok].utilidad_sem_sugerido - recos[ok].utilidad_sem_mantener).sum())

    # ---- paneles embebidos: top-N por dirección (alta + stock) ----
    stock_col = recos.disponible if "disponible" in recos.columns else recos.existencia
    base = recos[(recos.confianza == "alta") & (stock_col > 0)]
    con_panel = set()
    for d in ["SUBIR", "BAJAR", "MANTENER"]:
        con_panel |= set(base[base.direccion == d]
                         .nlargest(n_paneles, "utilidad_sem_mantener").codigo)
    con_panel |= set(base.nlargest(n_paneles, "utilidad_sem_mantener").codigo)
    # + TOP 200 GLOBAL de lo que se muestra (sin filtro de confianza/stock):
    # las primeras filas del reporte siempre deben abrir su detallado
    con_panel |= set(recos.nlargest(n_top, "utilidad_sem_mantener").codigo)
    # + TODOS los de "💲 Costo movió" (usuario 2026-07-31): un movimiento de
    # costo dispara revisión y la revisión necesita el detallado individual
    ruta_rc = os.path.join(BASE, "out", "revision_costos.csv")
    if os.path.exists(ruta_rc):
        rc_pan = pd.read_csv(ruta_rc)
        rc_pan = rc_pan[pd.to_datetime(rc_pan.fecha_ultimo) >=
                        pd.Timestamp.today().normalize() - pd.Timedelta(days=21)]
        # tope: los 100 de mayor utilidad (2026-08-04: una ola de 224 eventos
        # infló el HTML a 17MB > límite de publicación de 16MB; el resto tiene
        # su detallado en el caché nocturno out/paneles/)
        rc_codigos = set(rc_pan.codigo) & set(recos.codigo)
        rc_top = (recos[recos.codigo.isin(rc_codigos)]
                  .nlargest(100, "utilidad_sem_mantener").codigo)
        con_panel |= set(rc_top)
    paneles, datos = [], {}
    for cod in con_panel:
        sid = _safe(cod)
        cuerpo, D = cuerpo_sku(cod, ctx,
                               volver_html='<a href="#" onclick="return volver()">← Volver al resumen</a>')
        paneles.append(f'<div class="wrap oculto" id="panel_{sid}">{cuerpo}'
                       f'<div class="foot">{PIE}</div></div>')
        datos[sid] = D

    # ---- filas: TODO el evaluable, orden por utilidad desc ----
    # peor caso del rango (95%) del escenario SUGERIDO: "esperado + peor caso"
    # (aprobado 2026-07-28; sustituye a la probabilidad cruda, que sobrevende)
    esc_all = pd.read_csv(os.path.join(BASE, "out", "escenarios.csv"))
    peor_map = (recos[["codigo", "cambio_pct"]]
                .merge(esc_all[["codigo", "escenario_pct", "d_util_lo"]],
                       left_on=["codigo", "cambio_pct"], right_on=["codigo", "escenario_pct"],
                       how="left").set_index("codigo").d_util_lo)
    # tooltip de SENSIBILIDAD AL PRECIO (usuario 2026-07-29): qué significa el
    # nivel del producto en el comparativo de 3 niveles del catálogo
    ruta_niv = os.path.join(BASE, "data", "eps_por_sku.parquet")
    if os.path.exists(ruta_niv):
        _eps_sku = pd.read_parquet(ruta_niv)
        niv_map = _eps_sku.set_index("codigo").nivel
        # ε de cada segmento DINÁMICAS (auditoría 2026-07-30 K2: estaban
        # hardcodeadas y el modelo las re-estima cada corrida)
        _seg = (_eps_sku[_eps_sku.nivel == "segmento"]
                .groupby("segmento").eps.first())
        E_BAJO, E_MED, E_ALTO = (_seg.get("bajo", -0.8), _seg.get("medio", -0.9),
                                 _seg.get("alto", -1.0))
    else:
        niv_map = pd.Series(dtype=object)
        E_BAJO, E_MED, E_ALTO = -0.8, -0.9, -1.0
    _NIV = {"bajo": "POCO SENSIBLE (nivel 1 de 3)",
            "medio": "SENSIBILIDAD MEDIA (nivel 2 de 3)",
            "alto": "MUY SENSIBLE (nivel 3 de 3)"}
    def _tt_eps(x):
        seg = str(getattr(x, "segmento", "") or "")
        e_ = float(getattr(x, "eps", -1.0))
        _nv = str(niv_map.get(x.codigo, ""))
        fin = ("Su elasticidad está afinada con la respuesta observada en SUS propios "
               "cambios de precio (≥3 eventos reales)." if "capa SKU" in _nv else
               ("Su elasticidad está afinada con los datos de su proveedor."
                if "proveedor" in _nv else ""))
        return (f"Sensibilidad al precio de este modelo: {_NIV.get(seg, seg)} — "
                f"elasticidad {e_:.2f}: si el precio sube 1%, su venta baja ~{abs(e_):.1f}%. "
                f"Comparativo de los 3 niveles del catálogo (por rotación): "
                f"1 poco sensible ({E_BAJO:.2f}) · 2 media ({E_MED:.2f}) · "
                f"3 muy sensible ({E_ALTO:.2f}). {fin}")
    r = recos.sort_values("utilidad_sem_mantener", ascending=False).copy()
    tiene_prov = ("proveedor" in r.columns
                  and (r.proveedor.fillna("") != "(pendiente censo)").any())
    # DISPARADOR DE REVISIÓN POR COSTO (usuario 2026-07-30): la vigía diaria
    # registra cada costo que sube/baja ≥2% en out/revision_costos.csv; aquí
    # se vuelve chip 🔺/🔻 + filtro '💲 Costo movió'. Vigencia: 21 días (un
    # ciclo) — después el ciclo ya re-decidió con el costo nuevo.
    ruta_rc = os.path.join(BASE, "out", "revision_costos.csv")
    costo_map = {}
    if os.path.exists(ruta_rc):
        rc = pd.read_csv(ruta_rc)
        rc = rc[pd.to_datetime(rc.fecha_ultimo) >=
                pd.Timestamp.today().normalize() - pd.Timedelta(days=21)]
        for _, e in rc.iterrows():
            costo_map[e.codigo] = [
                float(e.pct), str(e.fecha_ultimo),
                (round(100 * e.margen_antes, 1) if pd.notna(e.margen_antes) else None),
                (round(100 * e.margen_nuevo, 1) if pd.notna(e.margen_nuevo) else None),
                (str(e.tipo_prov) if "tipo_prov" in rc.columns and pd.notna(e.tipo_prov) else "")]
    # ---- SEMÁFORO COMPETITIVO (semaforo.py): campo 28 + vista dedicada ----
    sem_mod, filas3 = {}, []
    ruta_sm = os.path.join(BASE, "data", "competencia", "semaforo_modelo.parquet")
    if os.path.exists(ruta_sm):
        _sm = pd.read_parquet(ruta_sm)
        cod_sem = {"AMENAZA REAL": "A", "REMATE AJENO": "R", "ESPACIO": "E", "NEUTRO": "N"}
        for x in _sm.itertuples():
            sem_mod[x.codigo] = [("E" if x.match == "EXACTO" else "Q"),
                                 float(x.confiabilidad), float(x.gap_pct),
                                 cod_sem[x.semaforo], float(x.mejor_comp_usd),
                                 str(x.fuente), int(x.consenso_n)]
        _sp = pd.read_parquet(os.path.join(BASE, "data", "competencia", "semaforo.parquet"))
        dir_map = recos.set_index("codigo").direccion
        u_map = recos.set_index("codigo").u_sem_actual
        for x in _sp.itertuples():
            filas3.append([str(x.codigo), str(dir_map.get(x.codigo, "")[:1] or ""),
                           ("E" if x.match == "EXACTO" else "Q"),
                           float(x.confiabilidad), str(x.fuente), str(x.modelo_comp),
                           float(x.precio_comp_usd), float(x.subtotal_sys),
                           float(x.gap_pct),
                           (int(x.persistencia_d) if pd.notna(x.persistencia_d) else None),
                           str(x.stock_comp), int(x.consenso_n), cod_sem[x.semaforo],
                           round(float(u_map.get(x.codigo, 0) or 0), 1),
                           str(getattr(x, "base", "subtotal"))])
        # orden: NUESTRA venta semanal, mayor → menor (usuario 2026-08-10)
        filas3.sort(key=lambda f: -f[13])
        print(f"  semáforo competitivo: {len(sem_mod):,} modelos en el reporte "
              f"({len(filas3):,} pares en la vista)", flush=True)

    # ---- AUDITOR DE COSTOS (auditor.py): vista dedicada, agrupada por -----
    # PROVEEDOR porque las oleadas de costo llegan por proveedor (top 5 = 54%
    # de los eventos) y el factor vive en el proveedor (R²oos 0.51 vs 0.11 de
    # la huella de comportamiento). Sustituye al filtro '💲 Costo movió', que
    # sólo notificaba el cambio sin nada con qué juzgarlo (usuario 2026-08-14).
    filas4, prov4 = [], []
    try:
        import auditor
        AU = auditor.construir()
    except Exception as e:
        AU = pd.DataFrame()
        print(f"  auditor de costos: no disponible ({e})", flush=True)
    if len(AU):
        V_COD = {"APLICAR": "A", "FRENO": "F", "CONFIRMAR COSTO": "R",
                 "BAJA": "B", "YA APLICADO": "Y", "SIN DATO": "S"}
        def _n(v, d=None):
            return (None if v is None or pd.isna(v) else
                    (round(float(v), d) if d is not None else float(v)))
        def _s(v):
            # OJO: `NaN or ""` devuelve NaN (NaN es truthy) ⇒ str() pintaría "nan"
            return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
        for x in AU.itertuples():
            hist = None
            if pd.notna(getattr(x, "h_pass_through", np.nan)):
                hist = [_n(x.h_d_costo_pct, 1), _n(100 * x.h_pass_through, 0),
                        _n(x.h_d_venta_pct, 1), _n(x.h_d_margen_pts, 1)]
            filas4.append([
                str(x.codigo), _s(x.proveedor), V_COD.get(str(x.veredicto), "S"),
                _s(getattr(x, "razon_cod", "")), _n(x.pct, 1), _n(x.alza_precio_pct, 1),
                _n(x.factor_antes, 2), _n(x.precio_vig, 2), _n(x.precio_pol, 2),
                _n(x.dano_pct, 1), _s(x.confianza), _n(x.eps, 2),
                _n(x.u_sem_actual, 1), _n(x.utilidad_sem_mantener, 0),
                _n(x.n_clientes, 1), _n(100 * x.pct_linea if pd.notna(x.pct_linea) else None, 0),
                _n(x.meses_inv, 1), _n(100 * x.crecimiento if pd.notna(x.crecimiento) else None, 0),
                bool(getattr(x, "crec_sospechoso", False)), _n(x.comp_gap, 0),
                _s(x.comp_sem), hist, _n(x.dias, 0),
                _n(100 * x.margen_antes if pd.notna(x.margen_antes) else None, 1),
                _n(100 * x.margen_nuevo if pd.notna(x.margen_nuevo) else None, 1),
                _s(x.tipo_prov), (_safe(str(x.codigo)) if str(x.codigo) in con_panel else None),
                _s(getattr(x, "fuente_costo", "")),
            ])
        # resumen por proveedor: la oleada es la unidad de decisión
        gp = AU.assign(_ap=(AU.veredicto == "APLICAR").astype(int),
                       _fr=(AU.veredicto == "FRENO").astype(int),
                       _rv=(AU.veredicto == "REVISAR").astype(int)).groupby(
            AU.proveedor.fillna("(sin censo)"))
        for nm, g in gp:
            prov4.append([str(nm), int(len(g)), _n(g.pct.median(), 1),
                          _n(g.utilidad_sem_mantener.sum(), 0),
                          int(g._ap.sum()), int(g._fr.sum()), int(g._rv.sum())])
        prov4.sort(key=lambda p: -(p[3] or 0))
        filas4.sort(key=lambda f: -(f[13] or 0))
        vc = AU.veredicto.value_counts()
        print(f"  auditor de costos: {len(AU)} eventos · " +
              " · ".join(f"{k} {v}" for k, v in vc.items()) +
              f" · {len(prov4)} proveedores", flush=True)

    filas = []
    for _, x in r.iterrows():
        motivo = str(x.revisar) if pd.notna(x.revisar) and str(x.revisar) != "" else ""
        flags = 0
        if "frenar" in motivo:
            flags |= 1
            if frenos is not None and x.codigo in frenos.index \
                    and str(frenos.loc[x.codigo].estado) == "reabastecido":
                flags |= 2
        if x.codigo in con_panel:
            flags |= 4
        if bool(getattr(x, "holdout", False)):
            flags |= 8
        # amplitud territorial (forecast AWS por sucursal): números crudos;
        # el chip se arma en JS con umbrales unificados
        frac = getattr(x, "aws_suc_alza_frac", np.nan)
        top = getattr(x, "aws_suc_top_share", np.nan)
        act = getattr(x, "aws_suc_activas", np.nan)
        terr = ([int(act), int(round(float(frac) * act)), int(round(100 * float(top)))]
                if pd.notna(act) and pd.notna(frac) and pd.notna(top) else None)
        # escaparate web (API de BI, informativo): [vistas, conv%, mediana del
        # sitio, días de ventana]; el chip se arma en JS con los mismos
        # umbrales que la mención de escenarios.py (≥100 vistas, ≤½ / ≥2× med)
        vis = getattr(x, "web_vistas", np.nan)
        web = ([int(vis), float(getattr(x, "web_conv_pct", np.nan)),
                float(getattr(x, "web_conv_med", np.nan)),
                int(getattr(x, "web_dias", 0))]
               if pd.notna(vis) and pd.notna(getattr(x, "web_conv_pct", np.nan))
               and pd.notna(getattr(x, "web_conv_med", np.nan)) else None)
        filas.append([
            x.codigo, _safe(x.codigo), x.direccion[0],           # S/B/M
            str(x.rol or ""), x.confianza[0],                     # a/m/b
            str(getattr(x, "proveedor", "") or ""),
            round(float(x.precio_actual), 2), round(float(x.precio_sugerido), 2),
            round(float(x.u_sem_actual), 0), round(float(x.u_sem_p10), 0),
            round(float(x.u_sem_p90), 0), round(100 * float(x.margen_actual), 0),
            (round(float(x.meses_inv), 1) if pd.notna(x.meses_inv) else None),
            (round(float(x.reposicion), 0) if pd.notna(getattr(x, "reposicion", np.nan)) else None),
            (round(100 * float(x.crecimiento), 0) if pd.notna(x.crecimiento) else None),
            ("" if pd.isna(getattr(x, "meses_alza", "")) else str(getattr(x, "meses_alza", ""))),
            round(float(x.utilidad_sem_sugerido - x.utilidad_sem_mantener), 0),
            motivo, flags, terr, web,
            str(getattr(x, "meses_fuente", "") or ""),
            (round(float(peor_map.get(x.codigo, np.nan)), 0)
             if pd.notna(peor_map.get(x.codigo, np.nan)) else None),
            {"grumosa (lumpy)": "L", "intermitente": "I", "errática": "E",
             "suave": "S"}.get(str(getattr(x, "clase_serie", "")), ""),
            _tt_eps(x),
            costo_map.get(x.codigo),
            _clasif_remate(x),
            (round(float(x.ancla_arrastre)) if "ancla" in str(x.revisar or "")
             and pd.notna(getattr(x, "ancla_arrastre", None)) else None),
            # la mezcla de canal NO lleva chip en el resumen (usuario
            # 2026-08-01: "ya son demasiadas etiquetas") — trabaja en el fondo
            # (confianza) y se explica en el DETALLADO cuando fue determinante
            # 28 = semáforo competitivo [match,conf,gap,sem,mejor,fuente,n]
            sem_mod.get(x.codigo),
        ])
    provs = sorted(set(f[5] for f in filas if f[5])) if tiene_prov else []
    # OJO: índice explícito, no f[-1] — cada campo nuevo al final lo rompía
    # (2026-08-01: contaba anclas en vez de costos). 25 = costoMov.
    n_costo = sum(1 for f in filas if f[25])

    # ---- SEGUNDA CAPA: dormidos (dormidos.py), sin los de COMPRAS ----
    filas2, n_dorm, cap_dorm = [], 0, 0.0
    ruta_d = os.path.join(BASE, "out", "segunda_capa_dormidos.csv")
    if os.path.exists(ruta_d):
        dd = pd.read_csv(ruta_d)
        dd = dd[dd.direccion.isin(["REACTIVAR", "EVALUAR CONTINUIDAD", "LIQUIDAR", "SOLO PROYECTO", "ESPERAR STOCK", "VENDE POR PRESENTACIÓN"])]
        # ENFOQUE (usuario 2026-07-27): solo dormidos con >8 semanas sin venta
        # y CON STOCK VENDIBLE — lo accionable por precio. El resto queda en el CSV.
        n_antes = len(dd)
        # reloj de MUERTE EFECTIVA (usuario 2026-07-31): >8 semanas sin venta
        # TENIENDO stock — las semanas sin stock no cuentan como dormancia
        col_reloj = "sem_muertas_stock" if "sem_muertas_stock" in dd.columns else "semanas_sin_venta"
        # ≥8, no >8 (ronda 3, A2): "desde la semana 8" incluye la semana 8 —
        # la misma frontera de la puerta de cadencia en dormidos.py
        dd = dd[(dd[col_reloj] >= 8) & (dd.stock_venta.fillna(0) > 0)]
        print(f"  dormidos en vista: {len(dd):,} de {n_antes:,} "
              f"(enfoque ≥8 sem MUERTAS CON STOCK y stock vendible hoy)", flush=True)
        col_orden = ("valor_inventario" if "valor_inventario" in dd.columns
                     else "capital_atrapado")
        dd = dd.sort_values(col_orden, ascending=False)
        n_dorm, cap_dorm = len(dd), float(dd.capital_atrapado.sum())
        # detallado embebido para los 100 dormidos de mayor capital atrapado
        dorm_panel = set()
        ruta_ex = os.path.join(BASE, "data", "reporte61", "existencias_sem.parquet")
        exist_d = pd.read_parquet(ruta_ex) if os.path.exists(ruta_ex) else None
        for _, xd in dd.dropna(subset=["precio_epoca_viva"]).head(100).iterrows():
            try:
                cuerpo_d, D_d = cuerpo_dormido(
                    xd.codigo, ctx, xd, exist=exist_d,
                    volver_html='<a href="#" onclick="return volver()">← Volver al resumen</a>')
            except Exception:
                continue
            sid = _safe(xd.codigo)
            paneles.append(f'<div class="wrap oculto" id="panel_{sid}">{cuerpo_d}'
                           f'<div class="foot">{PIE}</div></div>')
            datos[sid] = D_d
            dorm_panel.add(xd.codigo)
        for _, x in dd.iterrows():
            if pd.isna(x.codigo) or pd.isna(x.precio_epoca_viva):
                continue
            p = getattr(x, "proveedor", "")
            # historia de costos (solo QUEDÓ CARO / REACTIVAR): F2, película 24m
            hist = ""
            if x.direccion == "REACTIVAR":
                lab = getattr(x, "incremento_laboratorio", None)
                compro = getattr(x, "compramos_tras_cambio", None)
                n_cc = getattr(x, "n_cambios_costo_24m", None)
                if lab is True or lab == True:
                    hist = "LAB"
                elif compro is True or compro == True:
                    hist = "REAL"
                elif pd.isna(n_cc) or float(n_cc or 0) == 0:
                    hist = "PURA"
                else:
                    hist = "SC"
            # panel embebido para el TOP 100 por capital atrapado
            sid_d = ""
            if x.codigo in dorm_panel:
                sid_d = _safe(x.codigo)
            filas2.append([
                str(x.codigo), x.direccion, ("" if pd.isna(p) else str(p)),
                x.ultima_venta,
                # reloj de MUERTE EFECTIVA en la celda (ronda 3, R3-8): la vista
                # filtra por semanas muertas CON stock — mostrar ese mismo número
                int(getattr(x, "sem_muertas_stock", x.semanas_sin_venta)
                    if pd.notna(getattr(x, "sem_muertas_stock", None))
                    else x.semanas_sin_venta),
                round(float(x.u_sem_epoca_viva), 1),
                round(float(x.precio_epoca_viva), 2),
                (round(float(x.precio_hoy), 2) if pd.notna(x.precio_hoy) else None),
                (round(float(x.delta_precio_pct), 1) if pd.notna(x.delta_precio_pct) else None),
                (round(float(x.precio_sugerido), 2) if pd.notna(x.precio_sugerido) else None),
                (round(float(x.stock_venta), 0) if pd.notna(x.stock_venta) else None),
                round(float(x.capital_atrapado), 0),
                str(x.explicacion),
                hist, sid_d,
                (round(float(x.meses_stock_era), 1)
                 if "meses_stock_era" in dd.columns and pd.notna(getattr(x, "meses_stock_era", None)) else None),
                _clasif_remate(x),
                # 17 = competencia más barata (exacto o similar) — dormidos
                (sem_mod.get(x.codigo) if sem_mod.get(x.codigo)
                 and sem_mod[x.codigo][2] < 0 else None),
            ])

    # ---- SIMULADOR (3ª sección): datos por SKU con la MISMA matemática del
    # motor (u=u0·f^ε, π=u·(neto·f−costo), IC95 con ε±1.96·se). neto0 y costo
    # se derivan de la utilidad y margen auditados: neto0=(π0/u0)/margen.
    ruta_e = os.path.join(BASE, "data", "eps_por_sku.parquet")
    eps_se = (pd.read_parquet(ruta_e).set_index("codigo").se
              if os.path.exists(ruta_e) else None)
    sim, sim_skip = [], 0
    for _, x in recos.iterrows():
        u0, mg = float(x.u_sem_actual), float(x.margen_actual)
        if not (u0 > 0 and 0 < mg < 1):
            sim_skip += 1
            continue
        marg_unit = float(x.utilidad_sem_mantener) / u0
        neto0 = marg_unit / mg
        se = (float(eps_se.get(x.codigo, np.nan)) if eps_se is not None else np.nan)
        if not np.isfinite(se):
            se = 0.13
        sim.append([
            x.codigo, str(getattr(x, "proveedor", "") or ""), str(x.rol or ""),
            round(u0, 1), round(neto0, 4), round(neto0 - marg_unit, 4),
            round(float(x.eps), 3), round(se, 3),
            (round(float(x.meses_inv), 1) if pd.notna(x.meses_inv) else None),
            (round(float(x.aws_u_prox_mes), 1)
             if "aws_u_prox_mes" in recos.columns and pd.notna(x.aws_u_prox_mes) else None),
            round(float(x.precio_actual), 2),
        ])

    # calibración para la celda de confiabilidad: tabla COMPLETA dirección ×
    # bucket de magnitud (F4: cada fila usa el bucket de SU cambio sugerido)
    cal_js = {}
    if calib is not None:
        for d, key in [("subida", "S"), ("bajada", "B")]:
            cal_js[key] = {}
            for _, c in calib[calib.direccion == d].iterrows():
                cal_js[key][c.bucket] = {"up": round(100 * float(c.uplift_portafolio), 1),
                                         "win": round(100 * float(c.tasa_exito), 0),
                                         "n": int(c.n)}

    # tasa base de la maniobra dominante (paso 4% por guardrail ⇒ cubeta 4-5%)
    # para el ENCABEZADO — es un hecho global, no varía por fila (2026-07-30)
    tasa_sub = cal_js.get("S", {}).get("4-5%")
    tasa_baj = cal_js.get("B", {}).get("4-5%")
    sub_subir = (f"paso +4% · ciclo 3 sem · ganó {tasa_sub['win']:.0f}% de {tasa_sub['n']:,}"
                 if tasa_sub else "paso +4% este ciclo (3 sem)")
    tt_subir = (f"Paso máximo +4% por ciclo de 3 semanas (guardrail). Tasa base histórica: "
                f"de {tasa_sub['n']:,} subidas de ~4% hechas en el pasado, ganó el "
                f"{tasa_sub['win']:.0f}% de las veces vs no haber movido el precio."
                if tasa_sub else "")
    sub_bajar = (f"revertir · rotar · no vende · ganó {tasa_baj['win']:.0f}% de {tasa_baj['n']:,}"
                 if tasa_baj else "revertir / rotar sobrestock / no se vende")
    tt_bajar = (f"Motivos: revertir aumentos dañinos, rotar sobrestock, o no se vende al precio "
                f"actual. Tasa base histórica: de {tasa_baj['n']:,} bajadas de ~4%, ganó el "
                f"{tasa_baj['win']:.0f}% (las bajadas suelen COSTAR utilidad puntual: son "
                f"política de capital/mercado, no de utilidad inmediata)." if tasa_baj else "")

    head = f"""<meta charset="utf-8">
<title>Motor de Precio Óptimo v3 — todos los modelos ({corte})</title>
{ESTILOS}
<style>
  /* Rejilla fija del reporte: filas alineadas, misma altura, sin scroll horizontal.
     Scope a las tablas-lista (#cuerpo/#cuerpoD) para no tocar los paneles embebidos. */
  .wrap{{max-width:1560px}}
  #vista-motor .card table, #vista-dormidos .card table{{table-layout:fixed;width:100%}}
  #vista-motor .card td, #vista-dormidos .card td{{padding:6px 8px;font-size:12.5px;overflow:hidden;vertical-align:middle}}
  #vista-motor .card th, #vista-dormidos .card th{{padding:7px 8px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
  #cuerpo tr{{height:56px}}
  #cuerpoD tr{{height:62px}}
  #cuerpo .rng, #cuerpoD .rng{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}}
  #cuerpo .badge, #cuerpoD .badge{{white-space:nowrap;font-size:9px;padding:2px 5px}}
  /* código: las etiquetas SIEMPRE visibles — envuelven a más líneas cuando
     aplique (la altura de fila es mínimo, crece solo si hace falta) */
  #cuerpo td:first-child, #cuerpoD td:first-child{{white-space:normal;line-height:1.9}}
  #cuerpo td:first-child .badge, #cuerpoD td:first-child .badge{{display:inline-block;line-height:1.3;margin:1px 0;vertical-align:middle}}
  .clamp3{{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
          white-space:normal;line-height:1.35}}
  /* combo de proveedor: búsqueda por texto + selección múltiple */
  .prov-menu{{position:absolute;top:calc(100% + 4px);left:0;z-index:60;background:var(--blanco);
             border:1px solid var(--borde);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12);
             width:340px;max-height:330px;overflow-y:auto;padding:4px}}
  .prov-it{{display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:12.5px;
           border-radius:6px;cursor:pointer;user-select:none}}
  .prov-it:hover{{background:var(--azul-suave)}}
  .prov-it input{{pointer-events:none}}
  .prov-limpia{{position:sticky;top:0;background:var(--blanco);border-bottom:1px solid var(--borde);
               font-weight:600;color:var(--azul)}}
</style>
<div class="wrap" id="resumen">
  <div class="top">
    <span style="font-size:20px;font-weight:700">Motor de Precio Óptimo <span style="color:var(--azul)">v3</span></span>
    <span style="font-size:12px;color:var(--gris)">TODOS los modelos evaluables ({len(filas):,}) · panel de {pan.semana.nunique()} semanas · ventas al {(pd.Timestamp(corte) + pd.Timedelta(days=6)).date()} <span class="hint" title="El motor trabaja con SEMANAS COMPLETAS (lunes a domingo). La última semana completa es la del {corte}, que cubre ventas hasta el domingo. La semana en curso entra al cerrar (así un lunes con 1 día de datos no sesga la demanda).">(semana del {corte})</span> · ε global {eps_g:.2f}</span>
  </div>
  <div class="kpis">
    <div class="card kpi" title="{tt_subir}"><div class="lbl">Subir precios</div><div class="val num" style="color:var(--verde)">{n_subir:,}</div><div class="sub hint" style="display:inline-block">{sub_subir}</div></div>
    <div class="card kpi" title="{tt_bajar}"><div class="lbl">Bajar precios</div><div class="val num" style="color:var(--rojo)">{n_bajar:,}</div><div class="sub hint" style="display:inline-block">{sub_bajar}</div></div>
    <div class="card kpi"><div class="lbl">Sin opinión</div><div class="val num" style="color:var(--gris)">{n_abs:,}</div><div class="sub">evidencia insuficiente ⇒ se conserva el precio</div></div>
    <div class="card kpi"><div class="lbl">Ganancia adicional estimada</div><div class="val num" style="color:var(--verde)">+{_usd(d_tot,0)}</div><div class="sub">/semana · confianza alta+media, puntual</div></div>
  </div>

  <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="btn btn-v btn-pri" id="v_motor" onclick="setVista('motor')">Motor principal</button>
    <button class="btn btn-v" id="v_dorm" onclick="setVista('dorm')">💤 Dormidos (2ª capa) · {n_dorm:,} · {_usd(cap_dorm,0)} atrapados</button>
    <button class="btn btn-v" id="v_sim" onclick="setVista('sim')">🧮 Simulador de escenarios</button>
    <button class="btn btn-v" id="v_comp" onclick="setVista('comp')">⚔ Competencia · {len(sem_mod):,} modelos</button>
    <button class="btn btn-v" id="v_aud" onclick="setVista('aud')" title="Restauración de factor por movimiento de costo: la política de SYSCOM (costo nuevo × mismo factor), con el payload para juzgar si el traslado va al 100%. Exento del paso de ±4pts porque es defensa de estructura, no optimización.">🧾 Auditor de costos · {len(filas4):,} eventos</button>
  </div>

  <div id="vista-motor">
  <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="btn btn-f btn-pri" id="f_T" onclick="setDir('T')">Todas</button>
    <button class="btn btn-f" id="f_S" onclick="setDir('S')">Subir</button>
    <button class="btn btn-f" id="f_B" onclick="setDir('B')">Bajar</button>
    <button class="btn btn-f" id="f_M" onclick="setDir('M')">Conservar</button>
    <select class="btn" id="sel_rol" onchange="pintar()">
      <option value="">Rol: todos</option><option value="KVI">KVI · imagen de precio</option><option value="Sales Driver">Sales Driver · jala ventas</option>
      <option value="Profit Gen">Profit Gen · deja margen</option><option value="Estándar">Estándar</option>
    </select>
    {'''<div style="position:relative" id="prov_box">
      <input class="btn" id="prov_txt" placeholder="Proveedor: todos" autocomplete="off"
             oninput="provPinta()" onfocus="provAbre(true)" style="min-width:230px">
      <div id="prov_menu" class="prov-menu oculto"></div>
    </div>''' if tiene_prov else '<span class="badge b-gris" title="corre: ./.venv/bin/python extract.py proveedores (requiere VPN) y regenera">PROVEEDOR: pendiente censo</span>'}
    <select class="btn" id="sel_conf" onchange="pintar()">
      <option value="">Confianza: todas</option><option value="a">alta</option>
      <option value="m">media</option><option value="b">baja</option>
    </select>
    <input class="btn" id="busca" placeholder="Buscar código…" oninput="pintar()" style="min-width:160px">
    <button class="btn" onclick="exportaCsv()" title="Descarga un .csv (modelo, precio_sugerido) con TODOS los modelos de la selección actual — respeta los filtros de dirección, rol, proveedor, confianza, costo y búsqueda">⬇ CSV</button>
    <button class="btn" id="btnApl" onclick="aplicarSel()" title="Aplica en el ERP los modelos marcados con la casilla — vía el puente local (aplicar.py servir), que valida P1/P3, guardrails y registra en monitoreo">🚀 Aplicar (0)</button>
  </div>

  <div class="card">
    <div class="sec-t">Modelos <span id="contador" style="color:var(--gris);font-weight:400"></span></div>
    <div class="sec-s">Orden: utilidad semanal actual · Clic en filas con ▸ para desplegar el panel completo
      (top {n_paneles} por dirección); el resto se genera con panel_sku.py</div>
    <table>
      {'<colgroup><col style="width:26%"><col style="width:4%"><col style="width:9%"><col style="width:6%"><col style="width:7%"><col style="width:15%"><col style="width:8%"><col style="width:4%"><col style="width:6%"><col style="width:15%"></colgroup>' if tiene_prov else '<colgroup><col style="width:29%"><col style="width:5%"><col style="width:7%"><col style="width:8%"><col style="width:17%"><col style="width:8%"><col style="width:5%"><col style="width:6%"><col style="width:15%"></colgroup>'}
      <thead><tr><th>Código</th><th>Rol</th>{'<th>Proveedor</th>' if tiene_prov else ''}<th>Precio actual</th><th title="paso del ciclo de 3 semanas">Precio sugerido</th><th>Impacto en utilidad / motivo</th>
          <th>Unid/sem</th><th>Margen</th><th title="Meses de stock (v3): stock en almacenes de venta ÷ demanda esperada mensual — forecast propio cuando pasa el filtro de credibilidad del modelo; si no, venta real sin meses de cero-por-stockout + tasa de proyectos (el hover de cada celda dice su fuente). +N BO = unidades pedidas en camino (backorder). Rojo ≥12 meses: guardrail sobrestock (no subir; con margen, bajar para rotar)">Stock</th><th title="Margen de error del pronóstico de venta de cada modelo (su rango p10-p90 vs lo esperado) y su nivel de confianza. Pasa el cursor sobre la celda para ver la tasa base histórica de la maniobra.">Confiabilidad</th></tr></thead>
      <tbody id="cuerpo"></tbody>
    </table>
  </div>
  </div><!-- /vista-motor -->

  <div id="vista-dormidos" class="oculto">
  <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="btn btn-d btn-pri" id="d_T" onclick="setDx('T')">Todos</button>
    <button class="btn btn-d" id="d_R" onclick="setDx('REACTIVAR')">Quedó caro → Reactivar</button>
    <button class="btn btn-d" id="d_S" onclick="setDx('EVALUAR CONTINUIDAD')">Murió sin cambio</button>
    <button class="btn btn-d" id="d_L" onclick="setDx('LIQUIDAR')">Bajó y no reaccionó</button>
    <input class="btn" id="buscaD" placeholder="Buscar código…" oninput="pintarD()" style="min-width:160px">
    <button class="btn" id="btnAplD" onclick="aplicarSelD()" title="Aplica en el ERP los dormidos marcados con la casilla — vía el puente local (aplicar.py servir), validados contra el precio sugerido de la 2ª capa y la regla P1/P3">🚀 Aplicar (0)</button>
  </div>
  <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <span class="rng" style="font-size:12px">Historia de costos (quedó caro):</span>
    <button class="btn btn-d btn-pri" id="c_T" onclick="setCx('T')">Todas</button>
    <button class="btn btn-d" id="c_LAB" onclick="setCx('LAB')" title="El costo subió pero NUNCA compramos a ese costo: el aumento fue teórico; el stock en mano conserva el costo viejo">🧪 Laboratorio</button>
    <button class="btn btn-d" id="c_REAL" onclick="setCx('REAL')" title="Hay compras posteriores al cambio: el costo nuevo es real">Costo real</button>
    <button class="btn btn-d" id="c_PURA" onclick="setCx('PURA')" title="La lista subió sin ningún cambio de costo en 24 meses">Subió sin cambio de costo</button>
    <button class="btn btn-d" id="c_SC" onclick="setCx('SC')" title="Hubo cambio de costo pero sin compra concluyente: el piso usa el costo del stock en mano">Costo nuevo sin compras</button>
  </div>
  <div class="card">
    <div class="sec-t">Dormidos — 2ª capa <span id="contadorD" style="color:var(--gris);font-weight:400"></span></div>
    <div class="sec-s">Modelos con historia que dejaron de vender: enfoque en >8 semanas sin venta y CON stock vendible (el resto vive en el CSV). El motor
      principal no los evalúa; aquí se diagnostica POR QUÉ murieron y qué hacer. Orden: VALOR DEL INVENTARIO (stock × costo en mano), mayor a menor.</div>
    <table>
      <colgroup><col style="width:19%"><col style="width:10%"><col style="width:9%"><col style="width:6%">
        <col style="width:13%"><col style="width:9%"><col style="width:9%"><col style="width:25%"></colgroup>
      <thead><tr><th>Código</th><th>Proveedor</th><th>Última venta</th><th>Vendía</th>
        <th>Precio: cuando vendía → hoy</th><th>Sugerido</th><th>Stock / capital</th>
        <th style="text-align:left">Explicación del estatus</th></tr></thead>
      <tbody id="cuerpoD"></tbody>
    </table>
  </div>
  </div><!-- /vista-dormidos -->

  <div id="vista-competencia" class="oculto">
  <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="btn btn-c btn-pri" id="s_T" onclick="setSem('T')">Todos</button>
    <button class="btn btn-c" id="s_A" onclick="setSem('A')" title="Barato + persistente ≥7d + su stock rotando + NUESTRA venta cayendo — el único caso que amerita defensa">🔴 Amenaza real</button>
    <button class="btn btn-c" id="s_R" onclick="setSem('R')" title="Más barato pero su stock estancado o precio efímero: es su problema de inventario, NO una posición de mercado — ignorar">🟠 Remate ajeno</button>
    <button class="btn btn-c" id="s_E" onclick="setSem('E')" title="Estamos ≥10% abajo del mejor competidor persistente — respalda subir">🟢 Espacio</button>
    <select class="btn" id="sel_match" onchange="pintarC()"><option value="">Match: todos</option><option value="E">100% exacto</option><option value="Q">Similar (equivalente)</option></select>
    <input class="btn" id="buscaC" placeholder="Buscar modelo…" oninput="pintarC()" style="min-width:160px">
  </div>
  <div class="card">
    <div class="sec-t">Competencia — semáforo con 4 firmas de evidencia <span id="contadorC" style="color:var(--gris);font-weight:400"></span></div>
    <div class="sec-s">Un precio ajeno NO es señal por sí solo: se valida con persistencia (≥7 días), la trayectoria de SU stock
      (¿vende o está atorado?), consenso entre fuentes y NUESTRO daño (venta cayendo). Exactos = voz firme; similares = contexto con su confiabilidad.</div>
    <table>
      <colgroup><col style="width:15%"><col style="width:4%"><col style="width:6%"><col style="width:9%"><col style="width:13%"><col style="width:8%"><col style="width:8%"><col style="width:7%"><col style="width:8%"><col style="width:9%"><col style="width:5%"><col style="width:10%"></colgroup>
      <thead><tr><th>Modelo SYSCOM</th><th>Dir</th><th title="Venta semanal NUESTRA del modelo (orden de la tabla, mayor a menor)">Vta/sem</th><th>Match</th><th>Competidor · modelo</th><th>Su precio</th><th>Nuestro subtotal</th><th>Gap</th><th title="Días que su precio lleva al nivel actual (±1%)">Persist.</th><th title="Trayectoria de su stock en la ventana del feed">Su stock</th><th title="Nº de fuentes ≤ +5% del mejor precio">Cons.</th><th>Semáforo</th></tr></thead>
      <tbody id="cuerpoC"></tbody>
    </table>
  </div>
  </div><!-- /vista-competencia -->

  <div id="vista-auditor" class="oculto">
  <div class="card" style="margin-bottom:12px">
    <div class="sec-t">🧾 Auditor de costos — ¿a qué modelos hay que cambiarles el precio porque cambió su costo?</div>
    <div class="sec-s"><b>Qué hace esta sección.</b> En SYSCOM el precio de un modelo se fija multiplicando su costo
      por un número —el <b>factor</b>—. Por ejemplo, un modelo con factor 2 y costo de $100 se vende en $200.
      La política es que <b>cuando el costo cambia, se conserva ese mismo factor</b>: si el costo pasa a $120, el
      precio pasa a $240. Aquí aparece cada modelo cuyo costo se movió en los <b>últimos 2 meses</b>, con el precio
      que le tocaría por esa regla y con la información para decidir si conviene aplicarla.<br><br>
      <b>Por qué está aparte del motor.</b> El motor decide si un precio está bien puesto y lo mueve máximo 4% por
      ciclo. Esto es otra cosa: es <b>recuperar un costo que ya subió</b>, y el 83% de los casos necesita más de 4%.
      No es optimizar, es no perder margen — por eso no le aplica ese límite.<br><br>
      <b>De dónde sale el cambio de costo.</b> Junto a cada porcentaje aparece la fuente del dato.
      <b>«lista del proveedor»</b> es lo que nos cobraría reponer el producto hoy, tomado de su lista y revisado a
      diario: es el dato bueno para decidir un precio. <b>«lo que pagamos»</b> es el costo real de las ventas de la
      semana; permite ver más atrás en el tiempo, pero un movimiento ahí puede venir de una compra puntual o de
      mezclar lotes de distinto costo, no de un cambio real de lista — por eso esos casos salen siempre como
      «confirmar con el proveedor» y nunca como «subir el precio».</div>
  </div>
  <div class="card" style="margin-bottom:12px">
    <div class="sec-t">Qué significa cada etiqueta</div>
    <div class="sec-s">Cada modelo recibe una sola recomendación, y siempre viene con el motivo escrito al lado.
      <b>El traslado es completo o no es: no existe «subir a medias».</b> Es la postura del negocio —es preferible que
      el cliente reclame que estamos caros a dejar el precio abajo— y coincide con lo que midieron nuestros propios
      datos: trasladar entre 0% y 50% del cambio de costo fue el peor resultado de todos (se perdió venta
      <i>y</i> margen a la vez).<br>
      <b>El precio de la competencia se muestra como referencia y nunca frena un aumento.</b> Aparece en el renglón
      para dar contexto, pero no cambia la recomendación.</div>
    <table>
      <colgroup><col style="width:22%"><col style="width:78%"></colgroup>
      <tbody>
        <tr><td><span class="badge b-verde">🟢 SUBIR el precio</span></td>
            <td>El costo subió y no encontramos ninguna razón para no pasarlo al precio. El precio sube el mismo
                porcentaje que subió el costo, que es lo que conserva el factor.</td></tr>
        <tr><td><span class="badge b-rojo">🔴 NO subir por ahora</span></td>
            <td>El costo subió, pero subir el precio haría más daño que bien. Siempre se dice por qué, y sólo
                por tres motivos: hay inventario para muchos meses y encarecerlo lo dejaría parado, el modelo está
                en remate o salida, o está apartado para medir.</td></tr>
        <tr><td><span class="badge b-azul">🔵 BAJAR el precio</span></td>
            <td>El costo bajó. Por la misma política el precio debería bajar lo mismo, pero conviene esperar dos
                ciclos para confirmar que la baja se sostiene: una baja pasajera no debe mover el precio de lista.</td></tr>
        <tr><td><span class="badge b-gris">🔎 Confirmar costo</span></td>
            <td>El costo que pagamos se movió, pero no confirmamos que el proveedor haya cambiado su lista. Puede
                ser una compra puntual o una mezcla de lotes. Hay que verificar el costo antes de tocar el precio.</td></tr>
        <tr><td><span class="badge b-gris">✓ Ya se hizo</span></td>
            <td>El precio ya se movió después de detectarse el cambio de costo. No hay nada pendiente.</td></tr>
        <tr><td><span class="badge b-gris">Faltan datos</span></td>
            <td>El modelo no está en la lista de precios que se revisa a diario, así que no tenemos su precio
                vigente y no se puede calcular cuánto debería moverse.</td></tr>
      </tbody>
    </table>
  </div>
  <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="btn btn-a btn-pri" id="a_T" onclick="setAud('T')">Todos</button>
    <button class="btn btn-a" id="a_A" onclick="setAud('A')" title="El costo subió y nada impide pasarlo al precio">🟢 Subir el precio</button>
    <button class="btn btn-a" id="a_F" onclick="setAud('F')" title="El costo subió, pero subir el precio haría más daño que bien — siempre se dice por qué">🔴 No subir por ahora</button>
    <button class="btn btn-a" id="a_B" onclick="setAud('B')" title="El costo bajó: por la misma política el precio debería bajar lo mismo">🔵 Bajar el precio</button>
    <button class="btn btn-a" id="a_R" onclick="setAud('R')" title="Se movió el costo que pagamos, pero no está confirmado que el proveedor cambiara su lista">🔎 Confirmar costo</button>
    <button class="btn btn-a" id="a_Y" onclick="setAud('Y')" title="El precio ya se movió después de detectarse el cambio de costo: sirve para ver qué tan rápido estamos reaccionando">✓ Ya se hizo</button>
    <select class="btn" id="sel_aprov" onchange="pintarA()"><option value="">Proveedor: todos</option></select>
    <input class="btn" id="buscaA" placeholder="Buscar modelo…" oninput="pintarA()" style="min-width:160px">
    <button class="btn" onclick="exportaAud()" title="Descarga un .csv (modelo, precio_por_politica, comentario) con la selección actual — para el flujo auditado del ERP">⬇ CSV</button>
  </div>
  <div class="card" style="margin-bottom:12px">
    <div class="sec-t">Los cambios agrupados por proveedor <span id="contadorAP" style="color:var(--gris);font-weight:400"></span></div>
    <div class="sec-s">Los proveedores no suben un modelo suelto: suben lotes completos, muchas veces todos en la
      misma dirección y el mismo día. Y el factor con el que se fija el precio es, en la práctica, una costumbre de
      cada proveedor más que algo propio de cada modelo. Por eso conviene decidir por proveedor y no de uno en uno.
      <b>Clic en un proveedor para desplegar todos sus modelos aquí mismo</b> (clic otra vez para cerrarlo). Se pueden
      abrir varios a la vez, y el desplegable muestra todos sus modelos aunque arriba tengas un filtro puesto.</div>
    <table>
      <colgroup><col style="width:34%"><col style="width:9%"><col style="width:11%"><col style="width:14%"><col style="width:11%"><col style="width:10%"><col style="width:11%"></colgroup>
      <thead><tr><th>Proveedor</th><th>Modelos</th><th title="El movimiento de costo típico del lote (la mediana): da la magnitud del alza o la baja que mandó este proveedor">Cuánto movió<br>el costo</th><th title="Suma de la ganancia semanal de los modelos de este proveedor: qué tanto está en juego en su decisión">$ por semana<br>en juego</th><th title="Cuántos de sus modelos conviene subir de precio">Subir</th><th title="Cuántos NO conviene subir por ahora, con motivo">No subir</th><th title="Cuántos necesitan que se confirme el costo, porque el movimiento se vio en el costo que pagamos y no en la lista del proveedor">Confirmar costo</th></tr></thead>
      <tbody id="cuerpoAP"></tbody>
    </table>
  </div>
  <div class="card">
    <div class="sec-t">Eventos de costo <span id="contadorA" style="color:var(--gris);font-weight:400"></span></div>
    <div class="sec-s">Ordenados por la ganancia semanal que deja cada modelo, de mayor a menor: los de arriba son
      los que de verdad mueven la aguja. Pasa el cursor sobre cualquier celda o etiqueta para ver de dónde sale el
      número; clic en el nombre del modelo (▸) para abrir su análisis completo.</div>
    <table>
      <colgroup><col style="width:14%"><col style="width:7%"><col style="width:6%"><col style="width:7%"><col style="width:11%"><col style="width:9%"><col style="width:7%"><col style="width:8%"><col style="width:7%"><col style="width:24%"></colgroup>
      <thead><tr><th>Modelo</th><th>Proveedor</th><th title="Tres datos: cuánto se movió el costo; de dónde salió ese dato («lista del proveedor» es lo que costaría reponerlo hoy, el bueno para decidir; «lo que pagamos» es el costo real de las ventas de la semana, hay que confirmarlo); y cuántos días lleva movido sin que el precio se ajuste. Nuestro ciclo de decisión es de 3 semanas: pasados 21 días se marca en rojo.">Cuánto se movió<br>el costo y hace cuánto</th><th title="Cuántas veces el costo es el precio. Ejemplo: factor 2 = un modelo que cuesta $100 se vende en $200. Conservarlo con el costo nuevo mueve el precio el mismo % que se movió el costo">Veces el costo<br>(factor)</th><th title="Precio de hoy y precio que le tocaría al conservar el mismo factor con el costo nuevo">Precio hoy → el que<br>tocaría por política</th>
        <th title="Cuántas unidades más o menos se esperaría vender con ese precio nuevo, según la sensibilidad al precio medida de este modelo. Si no hay una medición confiable, no se muestra">Qué pasaría con<br>la venta</th><th title="Unidades por semana y cuántos clientes distintos lo compran. Con 1 o 2 clientes el alza es una negociación, no un cambio de precio de mercado">Cuánto vende</th><th title="Meses de inventario que hay, y qué parte del volumen se vende por la tienda en línea en lugar de por vendedor (las alzas se sostienen mejor en línea)">Inventario<br>y canal</th><th title="Ganancia semanal que este modelo deja hoy: sirve para saber qué decisiones importan y cuáles no">Ganancia<br>por semana</th><th>Qué conviene hacer y por qué</th></tr></thead>
      <tbody id="cuerpoA"></tbody>
    </table>
  </div>
  </div><!-- /vista-auditor -->

  <div id="vista-sim" class="oculto">
  <div class="card" style="margin-bottom:12px">
    <div class="sec-t">🧮 Simulador de escenarios — ¿qué pasa si movemos el precio n%?</div>
    <div class="sec-s">Misma matemática auditada del motor: unidades = u0·(1+n%)^ε con la ε de cada modelo
      (segmento afinado por proveedor) y banda IC95 por la incertidumbre de ε. Alcance: todo el catálogo
      evaluable, uno o varios proveedores, o una lista de códigos. Es exploratorio — NO respeta guardrails;
      los avisos indican qué violaría.</div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start;padding:8px 14px 14px">
      <div>
        <div style="font-size:11px;font-weight:600;color:var(--gris);margin-bottom:4px">CAMBIO DE PRECIO</div>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="btn" id="sim_pct" type="number" value="10" step="0.5" min="-50" max="100" style="width:90px" onchange="simular()">
          <span style="font-weight:700">%</span>
          <button class="btn btn-d" onclick="simSet(-10)">−10</button>
          <button class="btn btn-d" onclick="simSet(-4)">−4</button>
          <button class="btn btn-d" onclick="simSet(4)">+4</button>
          <button class="btn btn-d" onclick="simSet(10)">+10</button>
        </div>
      </div>
      <div style="position:relative" id="simprov_box">
        <div style="font-size:11px;font-weight:600;color:var(--gris);margin-bottom:4px">ALCANCE: PROVEEDOR(ES)</div>
        <input class="btn" id="simprov_txt" placeholder="Todos los proveedores" autocomplete="off"
               oninput="simProvPinta()" onfocus="simProvAbre(true)" style="min-width:230px">
        <div id="simprov_menu" class="prov-menu oculto"></div>
      </div>
      <div style="flex:1;min-width:260px">
        <div style="font-size:11px;font-weight:600;color:var(--gris);margin-bottom:4px">O LISTA DE CÓDIGOS (separados por coma, espacio o enter — tiene prioridad)</div>
        <textarea class="btn" id="sim_cods" rows="2" style="width:100%;resize:vertical;font-family:inherit" placeholder="LBE5ACGEN2, TT101FTURBO…" oninput="simular()"></textarea>
      </div>
      <div style="align-self:flex-end"><button class="btn btn-pri" onclick="simular()">Simular</button></div>
    </div>
  </div>
  <div id="sim_out"></div>
  </div><!-- /vista-sim -->

  <div class="foot">{PIE} · reporte_top.py — todos los modelos + 2ª capa + simulador, filtros en vivo</div>
</div>
"""

    js = """
<div class="tip" id="tip"></div>
<script>
__JS_LIB__
const DATOS = __DATOS__;
const FILAS = __FILAS__;
const CAL = __CAL__;
// margen de error de todo el catálogo, ordenado: da el "lugar" de cada modelo
// (percentil) para el flotante de la columna Confiabilidad
const ERRS_CAT = FILAS.map(f => 100*(f[10]-f[9])/(2*Math.max(f[8],1e-9))).sort((a,b)=>a-b);
const ERR_MED = Math.round(ERRS_CAT[Math.floor(ERRS_CAT.length/2)]);
function pctlErr(e){ let lo=0, hi=ERRS_CAT.length; while(lo<hi){const m=(lo+hi)>>1; if(ERRS_CAT[m]<e)lo=m+1; else hi=m;} return Math.round(100*lo/ERRS_CAT.length); }
// etiqueta corta del motivo (cubre los 450 textos del motor; verificado
// contra out/recomendaciones.csv 2026-07-30 — 1 solo caso cae al recorte)
function motCorto(motivo){
  if (!motivo) return "";
  const v = motivo.toLowerCase();
  if (v.includes("abstención") || v.includes("abstencion")) return "ABSTENCIÓN";
  if (v.includes("ancla de canasta")) return "PROTEGER CANASTA";
  if (v.includes("sobrestock")) { const m = v.match(/\\((\\d+)/); return "SOBRESTOCK"+(m?" "+m[1]+"m":""); }
  if (v.includes("revertir")) return "REVERTIR ALZA";
  if (v.includes("kvi")) return "PROTEGER KVI";
  if (v.includes("vendiendo caro")) return "VENDE CARO";
  if (v.includes("frenar") || v.includes("freno")) return "FRENO";
  if (v.includes("en medición")) return "MEDICIÓN";
  return motivo.slice(0, 24).toUpperCase();
}
const TIENE_PROV = __TIENE_PROV__;
const PROVS = __PROVS__;
const SIM = __SIM__;
const MAX_VISIBLE = 500;
let dirAct = "T";
// (el filtro '💲 Costo movió' se retiró el 2026-08-14: sólo notificaba el
// cambio, igual que el chip 🔺/🔻 de cada fila. La vista '🧾 Auditor de costos'
// lo sustituye con el precio por política y el payload para decidir.)
const usd = v => "$" + Number(v).toLocaleString("en-US", {maximumFractionDigits: 2});
const usd0 = v => "$" + Number(v).toLocaleString("en-US", {maximumFractionDigits: 0});
function setDir(d){ dirAct = d;
  ["T","S","B","M"].forEach(x => document.getElementById("f_"+x).classList.toggle("btn-pri", x===d));
  pintar(); }

/* --- combo de proveedor: escribir para buscar + selección múltiple --- */
const provSel = new Set();
function provAbre(abrir){
  if (!TIENE_PROV) return;
  document.getElementById("prov_menu").classList.toggle("oculto", !abrir);
  if (abrir) provPinta();
}
function provPinta(){
  const q = document.getElementById("prov_txt").value.trim().toUpperCase();
  const lista = PROVS.filter(p => !q || p.toUpperCase().includes(q));
  let h = '<div class="prov-it prov-limpia">Proveedor: todos (limpiar selección)</div>';
  h += lista.slice(0, 200).map(p =>
    '<div class="prov-it" data-p="'+p.replace(/"/g,"&quot;")+'">'
    +'<input type="checkbox"'+(provSel.has(p)?' checked':'')+'> <span>'+p+'</span></div>').join("");
  if (!lista.length) h += '<div class="prov-it" style="color:var(--gris)">sin coincidencias</div>';
  document.getElementById("prov_menu").innerHTML = h;
  provEtiqueta(); pintar();
}
function provToggle(p){
  provSel.has(p) ? provSel.delete(p) : provSel.add(p);
  provPinta();
}
function provLimpia(){
  provSel.clear();
  document.getElementById("prov_txt").value = "";
  provPinta();
}
function provEtiqueta(){
  const t = document.getElementById("prov_txt");
  t.placeholder = !provSel.size ? "Proveedor: todos"
    : (provSel.size === 1 ? [...provSel][0] : provSel.size + " proveedores seleccionados");
}
document.getElementById("prov_menu") && document.getElementById("prov_menu").addEventListener("click", e => {
  const it = e.target.closest(".prov-it");
  if (!it) return;
  if (it.classList.contains("prov-limpia")) provLimpia(); else provToggle(it.dataset.p);
});
document.addEventListener("click", e => {
  const caja = document.getElementById("prov_box");
  const ruta = e.composedPath ? e.composedPath() : [e.target];
  if (caja && !ruta.includes(caja)) provAbre(false);
});
function celdas(f){
  const [cod,sid,dir,rol,conf,prov,pa,ps,u,p10,p90,mg,minv,repo,crec,alza,dutil,motivo,flags,terr,web,mesesF,peor,cls,ttEps,costoMov,remN,anclaArr,cmp] = f;
  let badge = dir==="S" ? '<span class="badge b-verde">SUBIR</span>'
            : dir==="B" ? '<span class="badge b-rojo">BAJAR</span>'
            : (motivo ? '<span class="badge b-ambar" title="'+motivo+'">EN REVISIÓN</span>'
                      : '<span class="badge b-gris">CONSERVAR</span>');
  if (flags & 1) badge += ' <span class="badge b-azul" title="'+motivo+'">⏸ FRENO</span>';
  if (flags & 2) badge += ' <span class="badge b-rojo">🔔 RE-DECIDIR</span>';
  if (motivo && motivo.includes("en medición")) badge += ' <span class="badge b-azul" title="Este modelo tiene un cambio de precio en MEDICIÓN (periodo dinámico según su tipo de venta y cantidad de datos: 6-12 semanas). No se re-toca hasta tener veredicto — salvo freno, reversión o sobrestock">🧪 MEDICIÓN</span>';
  if (flags & 8) badge += ' <span class="badge b-gris" title="Holdout experimental (15% aleatorio de los elegibles): este ciclo NO se aplica y sirve de grupo de control; se aplica el siguiente. Es lo que permite medir el efecto real de los cambios sin sesgo de selección.">🎲 CONTROL</span>';
  // etiquetas de DEMANDA unificadas (cuantificadas; detalle explícito en el panel)
  if (crec !== null && Math.abs(crec) >= 5)
    badge += ' <span class="badge '+(crec>0?'b-azul':'b-ambar')+'" title="Demanda '+(crec>0?'creciendo':'cayendo')+': cambio mediano mes vs mes de la venta recurrente (últimos 6 meses cerrados'+(alza?'; '+alza+' meses al alza':'')+')">'+(crec>0?'▲ +':'▼ ')+crec+'%/mes</span>';
  if (terr && (dir==="S" || dir==="B")) {
    const [act, up, top] = terr;
    if (act === 1)
      badge += ' <span class="badge b-ambar" title="Venta concentrada: toda su venta reciente sale de una única sucursal — probable cliente/proyecto local, no mercado general (forecast AWS por sucursal)">📍 1 SUC.</span>';
    else if (top >= 60)
      badge += ' <span class="badge b-ambar" title="Venta concentrada: la sucursal más grande concentra '+top+'% de la venta reciente entre '+act+' activas — leer el movimiento con cautela (forecast AWS por sucursal)">📍 '+top+'% · 1 SUC.</span>';
    if (act >= 3 && up/act >= 0.5)
      badge += ' <span class="badge b-verde" title="Demanda ancha: AWS pronostica alza en '+up+' de sus '+act+' sucursales activas — el movimiento nacional tiene soporte territorial">🗺️ '+up+'/'+act+' SUC.</span>';
  }
  if (web && (dir==="S" || dir==="B")) {
    const [vis, conv, cmed, dias] = web;
    if (vis >= 100 && conv <= cmed/2)
      badge += ' <span class="badge b-ambar" title="Miran, no compran — escaparate web ('+dias+' días, informativo): '+vis.toLocaleString()+' vistas pero solo '+conv.toFixed(1)+'% termina en compra (mediana del sitio: '+cmed.toFixed(0)+'%) — hay demanda que NO se concreta; las ventas solas no ven esto">👁 WEB '+conv.toFixed(1)+'%</span>';
    else if (vis >= 100 && conv >= 2*cmed)
      badge += ' <span class="badge b-verde" title="Convierte muy bien — escaparate web ('+dias+' días, informativo): '+conv.toFixed(0)+'% de '+vis.toLocaleString()+' vistas termina en compra, el doble de la mediana del sitio ('+cmed.toFixed(0)+'%): poder de precio observado en la vitrina">🛒 WEB '+conv.toFixed(0)+'%</span>';
  }
  if (cmp){
    const [cm,cconf,cgap,csem,cmejor,cfte,cn] = cmp;
    if (cm === "E"){
      const col = csem==="A" ? "b-rojo" : (csem==="E" ? "b-verde" : "b-gris");
      const et = csem==="A" ? "AMENAZA" : (csem==="R" ? "REMATE AJENO" : (csem==="E" ? "ESPACIO" : ""));
      const txt = cgap < 0
        ? 'COMP '+Math.abs(cgap)+'% ABAJO' + (csem==="A" ? ' · REAL' : (csem==="R" ? ' · REMATA' : ''))
        : 'COMP '+cgap+'% ARRIBA';
      badge += ' <span class="badge '+col+'" title="⚔ Comparado contra '+cfte.toUpperCase()+' (match 100% del mismo modelo; el mejor precio entre '+cn+' fuente(s) en nivel similar): vende a $'+cmejor.toLocaleString()+' USD, '+(cgap>0?'+':'')+cgap+'% vs nuestro subtotal. Semáforo con 4 firmas (persistencia, su stock, consenso, nuestro daño): '+(et||'NEUTRO')+'. Análisis completo en el detallado y en la pestaña ⚔ Competencia">⚔ '+txt+'</span>';
    } else {
      badge += ' <span class="badge b-gris" title="≈ Comparado contra '+cfte.toUpperCase()+' — producto SIMILAR de otra marca (equivalente por atributos + vector + precio, confiabilidad '+Math.round(cconf*100)+'%): vende a $'+cmejor.toLocaleString()+' ('+(cgap>0?'+':'')+cgap+'%). NO es match 100% — contexto, no dato firme. Detalle en ⚔ Competencia">≈ COMP SIMILAR '+Math.round(cconf*100)+'%</span>';
    }
  }
  if (anclaArr) badge += ' <span class="badge b-azul" title="⚓ ANCLA DE CANASTA: sus compañeros de folio pierden ~$'+anclaArr.toLocaleString()+'/sem si este precio sube (elasticidad cruzada del estudio de pares-evento con filtro duro, corrida 2026-07-31 — regenerable con analisis_canasta.py estricto) — el SUBIR se bloqueó para proteger la canasta completa">⚓ ANCLA −$'+anclaArr.toLocaleString()+'/sem</span>';
  if (remN) badge += ' <span class="badge b-rojo" title="Producto en REMATE'+(remN.startsWith("R")?" nivel "+remN:"")+' (clasificación del ERP): ya no se comercializa — si vende, dejar agotar el stock sin mover precio; el canal de remate gobierna. SUBIR/BAJAR bloqueados por regla.">🏷️ REMATE'+(remN.startsWith("R")?" "+remN:"")+'</span>';
  if (costoMov) {
    const [cp, cf, m0, m1, tprov] = costoMov;
    const up = cp > 0;
    badge += ' <span class="badge '+(up?'b-rojo':'b-azul')+'" title="Vigía diaria ('+cf+'): el costo del proveedor '+(up?'subió':'bajó')+' '+(up?'+':'')+cp+'%'
      + (tprov ? ' · categoría: '+tprov : '')
      + (m0!==null && m1!==null ? ' — margen estimado al precio actual: '+m0+'% → '+m1+'%' : '')
      + '. '+(up?'Revisar precio: el margen se comprime (si rompe el piso costo+3pts, la defensa de margen ya alertó con la lista mínima).':'Oportunidad: el margen se amplió — evaluar capturar margen o bajar para ganar volumen.')
      + '">'+(up?'🔺 COSTO +':'🔻 COSTO ')+cp+'%</span>';
  }
  if (cls === "L") badge += ' <span class="badge b-gris" title="Venta IRREGULAR (serie grumosa/lumpy): compra esporádica y de tamaño impredecible — el pronóstico puntual es poco confiable; por regla su confianza se topa en MEDIA">⚡ IRREGULAR</span>';
  else if (cls === "I") badge += ' <span class="badge b-gris" title="Venta ESPORÁDICA (serie intermitente): semanas sin venta frecuentes, tamaño estable — pronóstico con cautela">ESPORÁDICA</span>';
  const rolB = (rol && rol !== "Estándar")
    ? ' <span class="badge b-gris" title="'+({"KVI":"KVI: producto que forma la imagen de precio del negocio","Sales Driver":"Sales Driver: jala ventas (trae clientes y volumen)","Profit Gen":"Profit Generator: deja margen"}[rol]||rol)+'">'+({"KVI":"KVI","Sales Driver":"SD","Profit Gen":"PG"}[rol]||rol)+'</span>' : "";
  const clic = (flags & 4) ? ' class="fila-sku" onclick="abrir(\\''+sid+'\\')"' : "";
  const marca = (flags & 4) ? "▸ " : "";
  // etiqueta corta del motivo para el barrido visual; el texto completo del
  // motor va en el flotante del chip (usuario 2026-07-30: resumen compacto,
  // explicación al hover / en el detallado)
  const mot = motCorto(motivo);
  let dcell;
  if (dir==="S") dcell = '<span class="badge b-verde" title="Utilidad adicional esperada al aplicar la sugerencia'+(peor!==null?'; peor caso del rango con 95% de confianza: '+(peor>=0?'+':'')+usd0(peor)+'/sem':'')+(motivo?' — '+motivo:'')+'">+'+usd0(dutil)+'/sem</span>'
    + '<span class="rng">'+(peor!==null?('peor: '+(peor>=0?'+':'')+usd0(peor)):'')
    + (mot?((peor!==null?' · ':'')+mot):'')+'</span>';
  else if (dir==="B") dcell = '<span class="badge b-ambar" style="font-weight:600" title="'+motivo+' — Δ '+usd0(dutil)+'/sem es el costo aceptado de la política (rotar capital / proteger mercado vale más que la utilidad puntual)">'+(mot||motivo)+'</span><span class="rng">Δ '+usd0(dutil)+'/sem</span>';
  else dcell = motivo ? '<span class="badge b-ambar" style="font-weight:600" title="'+motivo+'">'+(mot||motivo)+'</span>' : '<span class="rng">sin acción</span>';
  let inv = minv===null ? "—" : (minv<0 ? "neg." : Math.round(minv)+" m");
  if (minv!==null && minv>=12) inv = '<b style="color:var(--rojo)">'+inv+'</b>';
  if (repo) inv += '<span class="rng" title="Unidades pedidas en camino (backorder)">+'+Number(repo).toLocaleString()+' BO</span>';
  // CONFIABILIDAD = lo que varía por producto: margen de error de SU pronóstico
  // + SU nivel de confianza. La tasa base de la maniobra (cubeta dirección ×
  // tamaño del paso) es un hecho global: vive en el encabezado y aquí solo en
  // el tooltip (usuario 2026-07-30).
  const pct = pa > 0 ? Math.abs(ps/pa - 1) * 100 : 0;
  const bucket = pct >= 9 ? ">9%" : pct >= 5.5 ? "6-8%" : pct >= 3.5 ? "4-5%" : "2-3%";
  const c = (CAL[dir]||{})[bucket];
  const err = Math.round(100*(p90-p10)/(2*Math.max(u,1e-9)));
  const errTxt = err > 999 ? "±999%+" : "±"+err+"%";
  // el color sigue el NIVEL DE CONFIANZA del motor (integra clase de serie,
  // identificación y señales), no cortes absolutos del error — la mediana del
  // catálogo es ±100% y teñir por magnitud dejaría la columna monocromática
  const colErr = {a:"var(--verde)", m:"#b45309", b:"var(--rojo)"}[conf] || "var(--tinta)";
  const NIVEL = {a:"ALTA (nivel 3 de 3)", m:"MEDIA (nivel 2 de 3)", b:"BAJA (nivel 1 de 3)"};
  const precision = 100 - pctlErr(err);
  const ttConf =
    'MARGEN DE ERROR '+errTxt+': la venta real del ciclo suele caer dentro de ese rango alrededor de lo esperado. '
    + 'Este modelo pronostica mejor que el '+precision+'% del catálogo (mediana: ±'+ERR_MED+'%).\\n\\n'
    + 'CONFIANZA '+(NIVEL[conf]||"")+': el motor revisa 4 respaldos — historia (≥13 semanas con venta), '
    + 'base de clientes (≥3 distintos/sem), cambios de precio observados, y venta no dominada por proyectos. '
    + 'Comparativo de los 3 niveles: ALTA = los 4 respaldos y serie estable → actuar; '
    + 'MEDIA = 3 de 4, o venta irregular (por regla no puede ser alta) → actuar con paso prudente; '
    + 'BAJA = 2 o menos → el motor NO opina y conserva el precio.'
    + (c && pct > 0 ? '\\n\\nTASA BASE DE LA MANIOBRA: de '+c.n.toLocaleString()+' '+(dir==="S"?'subidas':'bajadas')+' de '+bucket+' hechas en el pasado, ganó '+c.win+'% de las veces.' : '');
  const NIVEL_C = {a:"ALTA", m:"MEDIA", b:"BAJA"};
  const confi = '<b class="num hint" style="color:'+colErr+'">'+errTxt+'</b><span class="rng">error de venta · <b class="hint" style="color:'+colErr+'">'+(NIVEL_C[conf]||"")+'</b></span>';
  const sug = dir==="M" ? usd(ps)+' <span style="color:var(--gris)">(=)</span>'
                        : '<b>'+usd(ps)+'</b>';
  const chk = (ps!==null && dir!=="M") ? '<input type="checkbox" '+(selApl.has(cod)?'checked ':'')+'data-c="'+cod+'" onclick="event.stopPropagation();tSel(this.dataset.c,this)" style="margin-right:6px;vertical-align:middle" title="Seleccionar para aplicar en el ERP"> ' : '';
  return '<tr'+clic+'><td title="'+cod+(motivo?' — '+motivo:'')+'">'+chk+marca+'<span style="font-weight:600;color:var(--azul)">'+cod+'</span> '+badge+'</td>'
    +'<td>'+(rolB||'<span class="rng">Estándar</span>')+'</td>'
    +(TIENE_PROV?('<td title="'+(prov||"")+'"><span class="rng" style="font-size:12px;text-align:left">'+(prov||"—")+'</span></td>'):"")
    +'<td class="num">'+usd(pa)+'</td><td class="num">'+sug+'</td><td title="'+ttEps+'">'+dcell+'</td>'
    +'<td class="num">'+Number(u).toLocaleString()+'<span class="rng">rango '+Number(p10).toLocaleString()+'–'+Number(p90).toLocaleString()+'</span></td>'
    +'<td class="num">'+mg+'%</td><td class="num"'+(mesesF?' title="Meses de stock v3 — demanda esperada: '+mesesF+' (el forecast propio — ensamble GBM+ingenuo, adoptado por duelo out-of-time — solo se usa cuando pasa el filtro de credibilidad del modelo; proyectos cuentan como rotación a tasa de ventana larga)"':'')+'>'+inv+'</td><td title="'+ttConf+'">'+confi+'</td></tr>';
}
function pintar(){
  const rol = document.getElementById("sel_rol").value;
  const conf = document.getElementById("sel_conf").value;
  const q = document.getElementById("busca").value.trim().toUpperCase();
  const sel = FILAS.filter(f =>
    (dirAct==="T" || f[2]===dirAct) && (!rol || f[3]===rol) &&
    (!provSel.size || provSel.has(f[5])) && (!conf || f[4]===conf) &&
    (!q || f[0].toUpperCase().includes(q)));
  document.getElementById("cuerpo").innerHTML = sel.slice(0, MAX_VISIBLE).map(celdas).join("");
  document.getElementById("contador").textContent =
    "— " + sel.length.toLocaleString() + " modelos" +
    (sel.length > MAX_VISIBLE ? " (mostrando los " + MAX_VISIBLE + " de mayor utilidad)" : "");
  selMotor = sel;  // selección vigente para exportar CSV (incluye TODO el filtro, no solo lo visible)
}
let selMotor = [];
const selAplD = new Set();
function tSelD(cod, el){ el.checked ? selAplD.add(cod) : selAplD.delete(cod);
  const b=document.getElementById("btnAplD"); if(b) b.textContent = "🚀 Aplicar ("+selAplD.size+")"; }
function aplicarSelD(){
  if (!selAplD.size) return alert("Marca la casilla de los dormidos a aplicar");
  const ms = []; FILAS2.forEach(f => { if (selAplD.has(f[0]) && f[9] !== null) ms.push({modelo: f[0], precio: f[9]}); });
  if (confirm("¿Aplicar "+ms.length+" precio(s) sugerido(s) de DORMIDOS en el ERP?\\n(el puente valida contra la 2ª capa y la regla P1/P3)")) _postApl(ms);
}
let semAct = "T";
function setSem(x){ semAct = x;
  ["T","A","R","E"].forEach(k => document.getElementById("s_"+k).classList.toggle("btn-pri", k===x));
  pintarC(); }
function pintarC(){
  const q = document.getElementById("buscaC").value.trim().toUpperCase();
  const mt = document.getElementById("sel_match").value;
  const sel = FILAS3.filter(f => (semAct==="T" || f[12]===semAct)
    && (!mt || f[2]===mt)
    && (!q || f[0].toUpperCase().includes(q) || f[5].toUpperCase().includes(q)));
  const SEM = {A:'<span class="badge b-rojo">🔴 AMENAZA REAL</span>', R:'<span class="badge b-ambar">🟠 REMATE AJENO</span>',
               E:'<span class="badge b-verde">🟢 ESPACIO</span>', N:'<span class="badge b-gris">NEUTRO</span>'};
  document.getElementById("cuerpoC").innerHTML = sel.slice(0,400).map(f => {
    const [cod,dir,mt_,cf,fte,mc,pc,sub,gap,per,stk,n,sm,usem,bs] = f;
    return '<tr><td><span style="font-weight:600;color:var(--azul)">'+cod+'</span></td>'
      +'<td>'+(dir==="S"?'<span class="badge b-verde">S</span>':dir==="B"?'<span class="badge b-rojo">B</span>':dir?'<span class="badge b-gris">M</span>':'—')+'</td>'
      +'<td class="num">'+(usem||0).toLocaleString()+'</td>'
      +'<td>'+(mt_==="E"?'<span class="badge b-azul">100%</span>':'<span class="badge b-gris" title="equivalente entre marcas">≈ '+cf+'</span>')+'</td>'
      +'<td title="'+mc+'"><span class="rng" style="font-size:11px">'+fte.toUpperCase()+' · '+mc.slice(0,18)+'</span></td>'
      +'<td class="num">$'+pc.toLocaleString()+'</td><td class="num"'+(bs==="lista"?' title="Modelo CON STOCK pero SIN venta reciente: no hay neto real — la base de comparación es su LISTA vigente"':'')+'>$'+sub.toLocaleString()+(bs==="lista"?'<span class="rng">lista°</span>':'')+'</td>'
      +'<td class="num" style="color:var(--'+(gap<0?'rojo':'verde')+')">'+(gap>0?'+':'')+gap+'%</td>'
      +'<td class="num">'+(per!==null?per+'d':'—')+'</td><td>'+stk+'</td><td class="num">'+n+'</td><td>'+SEM[sm]+'</td></tr>';
  }).join("");
  document.getElementById("contadorC").textContent = "— "+sel.length.toLocaleString()+" pares"
    +(sel.length>400?" (mostrando 400)":"");
}
/* ============== AUDITOR DE COSTOS (restauración de factor) ==============
   FILAS4: [cod, prov, veredicto, razon, dCosto, alzaPrecio, factor, pVig, pPol,
            dano, conf, eps, uSem, util, nCli, pctLinea, mesesInv, crec,
            crecSosp, compGap, compSem, hist, diasSinReacc, mAntes, mNuevo,
            tipoProv, sid]                                                   */
const FILAS4 = __FILAS4__, PROV4 = __PROV4__;
let audAct = "T", audProv = "";
// Etiquetas en lenguaje llano: dicen QUÉ HACER, no el nombre interno de la regla
const V_AUD = {
  A:'<span class="badge b-verde" title="El costo del proveedor subió y no hay ninguna razón para no pasarlo al precio. Por política el precio sube el mismo porcentaje que subió el costo.">🟢 SUBIR el precio</span>',
  F:'<span class="badge b-rojo" title="El costo subió, pero subir el precio de este modelo haría más daño que bien. La razón de abajo dice exactamente por qué.">🔴 NO subir por ahora</span>',
  B:'<span class="badge b-azul" title="El costo del proveedor BAJÓ. Por la misma política, el precio debería bajar el mismo porcentaje.">🔵 BAJAR el precio</span>',
  R:'<span class="badge b-gris" title="Vimos que el costo se movió, pero en el costo que pagamos en las ventas, no en la lista del proveedor. Puede ser una compra puntual o una mezcla de lotes. Hay que confirmar el costo antes de mover el precio.">🔎 Confirmar costo</span>',
  Y:'<span class="badge b-gris" title="El precio de este modelo ya se movió después de que se detectó el cambio de costo: no hay nada pendiente.">✓ Ya se hizo</span>',
  S:'<span class="badge b-gris" title="No se puede calcular: este modelo no está en la lista de precios que revisa la vigía diaria, así que no tenemos su precio vigente.">Faltan datos</span>'};
// la razón viaja como código y se arma aquí con los números de la propia fila
// (evita 0.30 MB de prosa repetida en 2,150 eventos: el HTML rebasaba los 16 MB)
function razonAud(c, fa, ap, dn, mi, cg, nc){
  // texto en lenguaje llano: sin jerga interna, entendible por quien nunca vio el motor
  const d = (dn===null) ? "no tenemos una medición confiable de cuánto afectaría a la venta"
                        : "se esperaría vender " + Math.abs(Math.round(dn)) + "% "
                          + (dn<0 ? "menos" : "más") + " unidades";
  const f = (fa===null) ? "?" : fa.toFixed(2);
  const a = (ap===null) ? "?" : (ap>0?"+":"")+ap.toFixed(1);
  switch(c){
    case "sf": return "El precio de este modelo se fija en <b>"+f+" veces su costo</b>. Con el costo nuevo, "
                    + "mantener ese mismo número da <b>"+a+"% de precio</b>. No hay nada que lo impida: "+d+".";
    case "cg": return "El costo que <b>pagamos</b> se movió, pero <b>no confirmamos</b> que el proveedor haya "
                    + "cambiado su lista — puede ser una compra puntual o una mezcla de lotes. Si el cambio es "
                    + "real, el precio subiría "+a+"% (para mantener el factor de "+f+" veces el costo) y "+d+". "
                    + "<b>Confirma el costo antes de mover el precio.</b>";
    case "so": return "Hay inventario para <b>"+Math.round(mi)+" meses</b>. Subir el precio dejaría ese "
                    + "inventario parado más tiempo. Si el margen lo permite, lo correcto sería <b>bajar</b> "
                    + "el precio para que rote, no subirlo.";
    case "rm": return "El ERP tiene este modelo clasificado <b>para remate o salida</b>: se está sacando del "
                    + "catálogo, así que no se le sube el precio.";
    case "ho": return "Este modelo está <b>apartado a propósito</b> para medir resultados sin tocarlo (grupo de "
                    + "control). Moverlo arruinaría la medición.";
    case "bj": return "El costo del proveedor <b>bajó</b>, así que por la misma política el precio debería bajar "
                    + "lo mismo. Conviene <b>esperar dos ciclos</b> para confirmar que la baja se sostiene: una "
                    + "baja de costo pasajera no debe mover el precio de lista. El destino es el mismo, solo se "
                    + "llega más despacio que cuando el costo sube.";
    case "ya": return "El precio de este modelo <b>ya se movió</b> después de detectarse el cambio de costo. "
                    + "No hay nada pendiente aquí.";
    default:   return "No tenemos el precio vigente de este modelo porque <b>no está en la lista que revisa la "
                    + "vigía diaria</b>, así que no se puede calcular cuánto debería moverse.";
  }
}
function setAud(x){ audAct = x;
  ["T","A","F","B","R","Y"].forEach(k => { const b=document.getElementById("a_"+k);
    if(b) b.classList.toggle("btn-pri", k===x); });
  pintarA(); }
// recibe el ÍNDICE, no el nombre: los nombres de proveedor traen &, comillas y
// apóstrofos que rompen el atributo onclick del HTML
const audAbierto = new Set();
function audProvSet(i){
  audAbierto.has(i) ? audAbierto.delete(i) : audAbierto.add(i);
  pintarAP();
}
function pintarAP(){
  // TODOS los proveedores, no un top-N: con el tope de 40 los que quedaban
  // fuera no se podían ni abrir. Van ordenados por $ en juego, así que los
  // que importan quedan arriba, y el desplegable de arriba sirve para buscar.
  document.getElementById("cuerpoAP").innerHTML = PROV4.map((p, i) => {
    const [nm,n,dc,ut,na,nf,nr] = p;
    const ab = audAbierto.has(i);
    const act = ab ? ' style="background:rgba(37,99,235,.08)"' : "";
    let fila = '<tr'+act+' onclick="audProvSet('+i+')" style="cursor:pointer" title="Clic para ver o esconder sus modelos">'
      +'<td><span style="font-weight:600">'+(ab?"▾ ":"▸ ")+nm+'</span></td><td class="num">'+n+'</td>'
      +'<td class="num" style="color:var(--'+(dc>0?'rojo':'verde')+')">'+(dc>0?'+':'')+dc+'%</td>'
      +'<td class="num">$'+(ut||0).toLocaleString()+'</td>'
      +'<td class="num">'+(na?'<span class="badge b-verde">'+na+'</span>':'—')+'</td>'
      +'<td class="num">'+(nf?'<span class="badge b-rojo">'+nf+'</span>':'—')+'</td>'
      +'<td class="num">'+(nr?'<span class="badge b-gris">'+nr+'</span>':'—')+'</td></tr>';
    if (ab){
      // TODOS sus modelos, sin tope y sin importar el filtro de arriba
      const ms = FILAS4.filter(f => f[1]===nm);
      fila += '<tr><td colspan="7" style="padding:0 0 14px 0">'
        + '<div class="sec-s" style="padding:6px 2px">Los <b>'+ms.length+'</b> modelo(s) de este proveedor '
        + 'con movimiento de costo, ordenados por la ganancia semanal que dejan. '
        + 'Clic en el nombre (▸) para abrir el análisis completo del modelo.</div>'
        + '<table style="margin:0">'
        + '<thead><tr><th>Modelo</th><th>Proveedor</th><th>Cuánto se movió el costo</th><th>Veces el costo</th>'
        + '<th>Precio hoy → el que tocaría</th><th>Qué pasaría con la venta</th><th>Cuánto vende</th>'
        + '<th>Inventario y canal</th><th>Ganancia por semana</th><th>Qué conviene hacer y por qué</th></tr></thead>'
        + '<tbody>' + ms.map(filaAud).join("") + '</tbody></table></td></tr>';
    }
    return fila;
  }).join("");
  document.getElementById("contadorAP").textContent =
    "— " + PROV4.length.toLocaleString() + " proveedores, del que más dinero pone en juego al que menos";
}
let selAud = [];
// DÍAS desde que se detectó el cambio de costo: se muestran SIEMPRE, no sólo
// cuando llevan mucho. Es el reloj de qué tan rápido reaccionamos, y es el
// único indicador de este tipo que no tiene referencia publicada en la
// industria: lo fijamos nosotros. Un ciclo de decisión son 3 semanas (21 días).
function diasTxt(d, vd){
  if (d===null) return "";
  const hecho = (vd==="Y");
  const col = hecho ? "--verde" : d>=21 ? "--rojo" : d>=7 ? "--ambar" : "--gris";
  const txt = hecho ? "se movió a los "+d+" día(s)"
            : d===0 ? "detectado hoy"
            : "lleva "+d+" día"+(d===1?"":"s")+" sin moverse";
  const tip = hecho
    ? "Días entre que se detectó el cambio de costo y hoy. El precio ya se movió dentro de ese lapso."
    : "Días que lleva el costo movido sin que el precio se ajuste. Nuestro ciclo de decisión es de 3 semanas (21 días): a partir de ahí se marca en rojo, porque cada día que pasa es margen que ya no se recupera.";
  return '<div class="rng" style="color:var('+col+')" title="'+tip+'">'+txt+'</div>';
}
// un solo renderizador de fila, usado por la tabla de eventos Y por el
// desplegable de cada proveedor (para que no se separen con el tiempo)
function filaAud(f){
    const [cod,prov,vd,raz,dc,ap,fa,pv,pp,dn,cf,ep,us,ut,nc,pl,mi,cr,cs,cg,csem,hi,dsr,ma,mn,tp,sid,fte] = f;
    // de dónde salió el movimiento: reposición (vigía, el titular) o COGS (panel)
    const fteTxt = fte==="COGS"
      ? '<span class="rng" title="El dato viene del costo que PAGAMOS en las ventas de la semana. Permite ver 2 meses atrás, pero un movimiento aquí puede venir de una compra puntual o de mezclar lotes de distinto costo: no prueba que el proveedor haya cambiado su lista.">lo que pagamos</span>'
      : (fte ? '<span class="rng" title="El dato viene de la LISTA DEL PROVEEDOR, o sea lo que nos costaría reponer el producto hoy. Es el bueno para decidir el precio, y se revisa todos los días.">lista del proveedor</span>' : "");
    // lo que de verdad le pasó a ESTE modelo la última vez: el mejor argumento
    // el traslado puede ser negativo (el precio se movió al revés que el costo):
    // decirlo con palabras, porque "le pasamos -47%" no se entiende
    const tras = hi ? (hi[1] < 0 ? 'movimos el precio en <b>dirección contraria</b>'
                     : hi[1] === 0 ? '<b>no</b> movimos el precio'
                     : 'le pasamos <b>'+hi[1]+'%</b> de ese cambio al precio') : "";
    const hist = hi ? '<div class="rng" style="margin-top:3px" title="La vez anterior que a este mismo modelo le movieron el costo. Es la mejor evidencia que existe, porque no es un pronóstico: es lo que de verdad pasó con este modelo. El porcentaje que «le pasamos» es qué parte del cambio de costo acabó reflejada en el precio: 100% sería pasarlo completo, 0% absorberlo todo.">'
        + '📜 La vez pasada: el costo se movió '+(hi[0]>0?'+':'')+hi[0]+'%, '+tras+', '
        + 'la venta hizo '+(hi[2]>0?'+':'')+hi[2]+'% y el margen '+(hi[3]>0?'+':'')+hi[3]+' puntos</div>' : "";
    const alerta = cs ? '<div class="rng" style="margin-top:3px;color:var(--ambar)" title="Hubo semanas en que no tuvimos inventario suficiente para cubrir lo que este modelo suele vender. Entonces su caída de venta no significa que la gente lo quiera menos: significa que no había producto. No hay que leerla como que «ya vende poco».">⚠ Ojo: vendió menos porque faltó inventario, no porque bajara la demanda</div>' : "";
    const nomb = sid ? '<span style="font-weight:600;color:var(--azul);cursor:pointer" onclick="abrir(\\''+sid+'\\')">▸ '+cod+'</span>'
                     : '<span style="font-weight:600">'+cod+'</span>';
    // ε × movimiento de precio. En una BAJA de costo el signo se invierte y es
    // una GANANCIA de volumen, no un daño — pintarlo como daño confundía.
    const CONF = {a:"medición sólida", m:"medición razonable", b:"medición débil"};
    const dano = (dn===null) ? '<span class="rng" title="No tenemos una medición confiable de qué tan sensible al precio es este modelo, así que preferimos no inventar un número. Sin esa medición, la decisión se apoya en el resto de las columnas.">no medible</span>'
      : '<span style="color:var('+(dn<=-15?'--rojo':dn<=-8?'--ambar':dn>0?'--verde':'--gris')+')" title="Este modelo pierde alrededor de '+Math.abs(ep).toFixed(1)+'% de venta por cada 1% que sube su precio. Con un movimiento de '+ap+'% eso da '+dn+'% de unidades. Calidad del dato: '+(CONF[cf.slice(0,1)]||cf)+'.'+(dn>0?' Es positivo porque el precio BAJA, así que se vendería más.':'')+'">'
      +(dn>0?'+':'')+dn+'%<span class="rng"> '+(cf.slice(0,1)==="a"?"✓✓":cf.slice(0,1)==="m"?"✓":"~")+'</span></span>';
    const comp = (cg===null) ? "" : '<div class="rng" style="margin-top:3px" title="Semáforo competitivo: '+csem+'">⚔ '+(cg>0?'+':'')+cg+'% vs competidor'+(csem==="AMENAZA REAL"?' 🔴':'')+'</div>';
    return '<tr><td>'+nomb+(tp?'<div class="rng" title="Tipo de proveedor (reportes.revision_precios)">'+tp+'</div>':'')+'</td>'
      +'<td><span class="rng">'+(prov||"—").slice(0,18)+'</span></td>'
      +'<td class="num" style="color:var(--'+(dc>0?'rojo':'verde')+')" title="Cuánto se movió el costo del proveedor">'+(dc>0?'+':'')+dc+'%'
      +'<div>'+fteTxt+'</div>'+diasTxt(dsr,vd)+'</td>'
      +'<td class="num" title="Factor = precio aplicable ÷ costo. Restaurarlo con el costo nuevo mueve el precio '+ap+'%">'+(fa!==null?fa.toFixed(2):"—")+'</td>'
      +'<td class="num">$'+(pv!==null?pv.toLocaleString():"—")+' → <b>$'+(pp!==null?pp.toLocaleString():"—")+'</b>'
      +(ap!==null?'<div class="rng" style="color:var(--'+(Math.abs(ap)>4?'ambar':'gris')+')"'
        +(Math.abs(ap)>4?' title="Es más de lo que el motor mueve en un ciclo (4%). Aquí sí se permite: no se está optimizando el precio, se está recuperando un costo que ya subió."':'')
        +'>'+(ap>0?'+':'')+ap+'%'+(Math.abs(ap)>4?' · más de 4%':'')+'</div>':'')+'</td>'
      +'<td class="num">'+dano+'</td>'
      +'<td class="num" title="'+(nc!==null&&nc<=2?'Con 1-2 clientes esto no es un precio de mercado: es una negociación con el cliente enfrente':'clientes distintos por semana')+'">'+(us!==null?us.toLocaleString():"—")
      +'<div class="rng"'+(nc!==null&&nc<=2?' style="color:var(--ambar)"':'')+'>'+(nc!==null?nc+" cli":"")+'</div></td>'
      +'<td class="num"><span class="rng" title="Meses que duraría el inventario actual al ritmo de venta de hoy. Si pasa de 12 meses no se sube el precio: ya hay demasiado producto y encarecerlo lo dejaría parado más tiempo.">'+(mi!==null?mi+" meses inv.":"—")+'</span>'
      +'<div class="rng" title="Qué parte de las ventas de este modelo entra por la tienda en línea en lugar de por un vendedor. Cuando la mayoría va por línea, un alza se sostiene mejor: por vendedor tiende a compensarse con más descuento.">'+(pl!==null?pl+"% en línea":"")+'</div></td>'
      +'<td class="num">$'+(ut||0).toLocaleString()+'</td>'
      +'<td>'+V_AUD[vd]+'<div class="rng" style="margin-top:3px">'
      +razonAud(raz,fa,ap,dn,mi,cg,nc)+'</div>'+hist+alerta+comp+'</td></tr>';
}
function pintarA(){
  const q = document.getElementById("buscaA").value.trim().toUpperCase();
  audProv = document.getElementById("sel_aprov").value;
  const sel = FILAS4.filter(f => (audAct==="T" || f[2]===audAct)
    && (!audProv || f[1]===audProv)
    && (!q || f[0].toUpperCase().includes(q)));
  selAud = sel;
  document.getElementById("cuerpoA").innerHTML = sel.slice(0,400).map(filaAud).join("");
  document.getElementById("contadorA").textContent = "— "+sel.length.toLocaleString()+" eventos"
    +(sel.length>400?" (mostrando 400)":"")
    +" · $"+sel.reduce((a,f)=>a+(f[13]||0),0).toLocaleString()+"/sem en juego";
  pintarAP();
}
function exportaAud(){
  const lineas = ["modelo,precio_por_politica,comentario"];
  // sólo APLICAR: los de "confirmar costo" quedan fuera a propósito, porque su
  // precio sugerido se calcula contra una base de COGS que puede no ser el costo
  // de reponer (ver PENDIENTE en docs/CAMBIOS_PARA_TI.md)
  selAud.forEach(f => { if (f[8] !== null && f[2]==="A"){
    lineas.push(f[0] + "," + f[8] + ",Auditor de costos v3 | ciclo __CORTE__ | factor "
                + f[6] + " restaurado | costo " + (f[4]>0?"+":"") + f[4] + "% | precio "
                + (f[5]>0?"+":"") + f[5] + "%");
  }});
  if (lineas.length === 1) return alert("La selección no tiene eventos con veredicto APLICAR o PARCIAL.");
  const blob = new Blob(["﻿" + lineas.join("\\n")], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "auditor_costos_" + new Date().toISOString().slice(0,10) + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
}
const selApl = new Set();
function tSel(cod, el){ el.checked ? selApl.add(cod) : selApl.delete(cod);
  const b=document.getElementById("btnApl"); if(b) b.textContent = "🚀 Aplicar ("+selApl.size+")"; }
function aplicarSel(){
  if (!selApl.size) return alert("Marca la casilla de los modelos a aplicar");
  const ms = []; FILAS.forEach(f => { if (selApl.has(f[0]) && f[7] !== null) ms.push({modelo: f[0], precio: f[7]}); });
  if (confirm("¿Aplicar "+ms.length+" cambio(s) de precio en el ERP?\\n(el puente valida P1/P3 y guardrails)")) _postApl(ms);
}
function aplicarUno(cod, p){ if (confirm("¿Aplicar "+cod+" a $"+p+" en el ERP?")) _postApl([{modelo: cod, precio: p}]); }
function _postApl(ms){
  fetch("http://127.0.0.1:8765/aplicar", {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify({modelos: ms})})
  .then(r => r.json())
  .then(j => { alert((j.resultados||[]).map(r => r.modelo+": "+(r.status||"?")).join("\\n") || ("Error: "+(j.error||"?"))); })
  .catch(() => alert("Sin puente local. En la máquina del motor corre:\\n  ./.venv/bin/python aplicar.py servir\\n(la API key vive ahí, nunca en este reporte). Desde el artifact publicado no funciona — abre el reporte local (out/reporte_precios.html)."));
}
function exportaCsv(){
  // modelo + precio sugerido + comentario para el campo del ERP (usuario
  // 2026-08-04: todo cambio aplicado debe quedar etiquetado como del motor)
  const lineas = ["modelo,precio_sugerido,comentario"];
  selMotor.forEach(f => { if (f[7] !== null){
    const pct = f[6] > 0 ? Math.round(1000*(f[7]/f[6]-1))/10 : 0;
    lineas.push(f[0] + "," + f[7] + ",Motor de Precios v3 | ciclo __CORTE__ | "
                + (pct>0?"+":"") + pct + "%");
  }});
  const etiqueta = {T:"todas", S:"subir", B:"bajar", M:"mantener"}[dirAct] || "seleccion";
  const blob = new Blob(["﻿" + lineas.join("\\n")], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "precios_" + etiqueta + "_" + new Date().toISOString().slice(0,10) + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
}
function abrir(sid){
  document.getElementById("resumen").classList.add("oculto");
  document.querySelectorAll('[id^="panel_"]').forEach(e => e.classList.add("oculto"));
  document.getElementById("panel_" + sid).classList.remove("oculto");
  renderSku(DATOS[sid]);
  window.scrollTo(0, 0);
}
function volver(){
  document.querySelectorAll('[id^="panel_"]').forEach(e => e.classList.add("oculto"));
  document.getElementById("resumen").classList.remove("oculto");
  window.scrollTo(0, 0);
  return false;
}
// ---- vista Dormidos (2ª capa) ----
const FILAS2 = __FILAS2__;
const FILAS3 = __FILAS3__;
let dxAct = "T";
function setVista(v){
  document.getElementById("vista-motor").classList.toggle("oculto", v!=="motor");
  document.getElementById("vista-dormidos").classList.toggle("oculto", v!=="dorm");
  document.getElementById("vista-sim").classList.toggle("oculto", v!=="sim");
  document.getElementById("vista-competencia").classList.toggle("oculto", v!=="comp");
  document.getElementById("vista-auditor").classList.toggle("oculto", v!=="aud");
  document.getElementById("v_comp").classList.toggle("btn-pri", v==="comp");
  document.getElementById("v_aud").classList.toggle("btn-pri", v==="aud");
  document.getElementById("v_motor").classList.toggle("btn-pri", v==="motor");
  document.getElementById("v_dorm").classList.toggle("btn-pri", v==="dorm");
  document.getElementById("v_sim").classList.toggle("btn-pri", v==="sim");
  if (v==="dorm") pintarD();
  if (v==="comp") pintarC();
  if (v==="sim") simular();
  if (v==="aud") audInit();
}
// el desplegable de proveedor se llena del propio lote de eventos
let audLleno = false;
function audInit(){
  if (!audLleno){
    const s = document.getElementById("sel_aprov");
    PROV4.forEach(p => { const o=document.createElement("option");
      o.value=p[0]; o.textContent=p[0].slice(0,34)+" ("+p[1]+")"; s.appendChild(o); });
    audLleno = true;
  }
  pintarA();
}

/* ================= SIMULADOR DE ESCENARIOS ================= */
/* SIM: [cod, prov, rol, u0, neto0, costo, eps, se, mesesInv, awsMes, precio] */
const simProvSel = new Set();
function simSet(p){ document.getElementById("sim_pct").value = p; simular(); }
function simProvAbre(abrir){
  document.getElementById("simprov_menu").classList.toggle("oculto", !abrir);
  if (abrir) simProvPinta();
}
function simProvPinta(){
  const q = document.getElementById("simprov_txt").value.trim().toUpperCase();
  const lista = PROVS.filter(p => !q || p.toUpperCase().includes(q));
  let h = '<div class="prov-it prov-limpia">Todos los proveedores (limpiar)</div>';
  h += lista.slice(0,200).map(p =>
    '<div class="prov-it" data-p="'+p.replace(/"/g,"&quot;")+'">'
    +'<input type="checkbox"'+(simProvSel.has(p)?' checked':'')+'> <span>'+p+'</span></div>').join("");
  document.getElementById("simprov_menu").innerHTML = h;
  const t = document.getElementById("simprov_txt");
  t.placeholder = !simProvSel.size ? "Todos los proveedores"
    : (simProvSel.size===1 ? [...simProvSel][0] : simProvSel.size+" proveedores");
  simular();
}
function simProvToggle(p){ simProvSel.has(p)?simProvSel.delete(p):simProvSel.add(p); simProvPinta(); }
function simProvLimpia(){ simProvSel.clear(); document.getElementById("simprov_txt").value=""; simProvPinta(); }
document.getElementById("simprov_menu") && document.getElementById("simprov_menu").addEventListener("click", e => {
  const it = e.target.closest(".prov-it");
  if (!it) return;
  if (it.classList.contains("prov-limpia")) simProvLimpia(); else simProvToggle(it.dataset.p);
});
document.addEventListener("click", e => {
  const c = document.getElementById("simprov_box");
  const ruta = e.composedPath ? e.composedPath() : [e.target];
  if (c && !ruta.includes(c)) simProvAbre(false);
});
const SEM_MES = 4.345, Z95 = 1.96;  // nivel de confianza 95% (usuario 2026-07-27)
function simular(){
  const out = document.getElementById("sim_out");
  if (!out) return;
  const P = parseFloat(document.getElementById("sim_pct").value);
  if (!isFinite(P) || P === 0){ out.innerHTML = '<div class="card" style="padding:16px;color:var(--gris)">Indica un % distinto de 0.</div>'; return; }
  const f = 1 + P/100;
  const cods = new Set(document.getElementById("sim_cods").value.toUpperCase().split(/[\\s,;]+/).filter(Boolean));
  const sel = SIM.filter(r => cods.size ? cods.has(r[0].toUpperCase())
                        : (!simProvSel.size || simProvSel.has(r[1])));
  if (!sel.length){ out.innerHTML = '<div class="card" style="padding:16px;color:var(--gris)">Ningún modelo evaluable coincide con el alcance.</div>'; return; }
  let u0T=0,u1T=0,pi0T=0,pi1T=0,loT=0,hiT=0,ing0T=0,ing1T=0,awsT=0,nAws=0;
  let nPiso=0,nKvi=0,nSobre=0;
  const movs=[];
  for (const r of sel){
    const [cod,prov,rol,u0,neto0,costo,eps,se] = r;
    const eLo=eps-Z95*se, eHi=eps+Z95*se;
    const u1=u0*Math.pow(f,eps);
    const pi0=u0*(neto0-costo), pi1=u1*(neto0*f-costo);
    const piA=u0*Math.pow(f,eLo)*(neto0*f-costo), piB=u0*Math.pow(f,eHi)*(neto0*f-costo);
    u0T+=u0; u1T+=u1; pi0T+=pi0; pi1T+=pi1;
    loT+=Math.min(piA,piB)-pi0; hiT+=Math.max(piA,piB)-pi0;
    ing0T+=u0*neto0; ing1T+=u1*neto0*f;
    if ((neto0*f-costo)/(neto0*f) < 0.03) nPiso++;
    if (rol==="KVI") nKvi++;
    if (P>0 && r[8]!==null && r[8]>=12) nSobre++;
    if (r[9]!==null){ awsT+=r[9]; nAws++; }
    movs.push([cod,prov,r[10],u0,u1,pi1-pi0]);
  }
  movs.sort((a,b)=>Math.abs(b[5])-Math.abs(a[5]));
  const d=pi1T-pi0T, col=d>=0?"var(--verde)":"var(--rojo)";
  const ciclos=Math.ceil(Math.abs(P)/4);
  const kpi=(lbl,val,sub)=>'<div class="card kpi"><div class="lbl">'+lbl+'</div><div class="val num">'+val+'</div><div class="sub">'+sub+'</div></div>';
  let avisos='';
  if (Math.abs(P)>4) avisos+='<span class="badge b-ambar">FUERA DE GUARDRAIL: ±4%/ciclo — se aplicaría en '+ciclos+' ciclos (~'+(ciclos*3)+' semanas)</span> ';
  if (nPiso) avisos+='<span class="badge b-rojo">'+nPiso.toLocaleString()+' modelos quedarían con margen &lt;3% (piso)</span> ';
  if (nKvi && P>0) avisos+='<span class="badge b-ambar">incluye '+nKvi.toLocaleString()+' KVIs (política: no subir sin demanda confirmada)</span> ';
  if (nSobre) avisos+='<span class="badge b-rojo">'+nSobre.toLocaleString()+' modelos en sobrestock ≥12m (regla: no subir)</span> ';
  const filasM = movs.slice(0,20).map(m =>
    '<tr><td><span style="font-weight:600;color:var(--azul)">'+m[0]+'</span></td>'
    +'<td class="rng" style="font-size:11px;text-align:left">'+(m[1]||"—")+'</td>'
    +'<td class="num">'+usd(m[2])+' → <b>'+usd(m[2]*f)+'</b></td>'
    +'<td class="num">'+Math.round(m[3]).toLocaleString()+' → '+Math.round(m[4]).toLocaleString()+'</td>'
    +'<td class="num" style="color:'+(m[5]>=0?'var(--verde)':'var(--rojo)')+';font-weight:700">'+(m[5]>=0?'+':'')+usd0(m[5])+'/sem</td></tr>').join("");
  out.innerHTML =
    '<div class="kpis">'
    + kpi("Modelos en el escenario", sel.length.toLocaleString(), (cods.size?'lista de códigos':(simProvSel.size? simProvSel.size+' proveedor(es)':'todo el catálogo evaluable')))
    + kpi("Unidades/sem", Math.round(u0T).toLocaleString()+' → '+Math.round(u1T).toLocaleString(),
          'con ε de cada modelo · '+(P>0?'−':'+')+Math.abs(100*(1-u1T/u0T)).toFixed(1)+'% volumen')
    + kpi("Ingreso neto/sem", '$'+Math.round(ing0T).toLocaleString()+' → $'+Math.round(ing1T).toLocaleString(),
          (ing1T>=ing0T?'+':'')+(100*(ing1T/ing0T-1)).toFixed(1)+'%')
    + kpi("Impacto en utilidad/sem", '<span style="color:'+col+'">'+(d>=0?'+':'')+usd0(d)+'</span>',
          'con 95% de confianza: '+(loT>=0?'+':'')+usd0(loT)+' a '+(hiT>=0?'+':'')+usd0(hiT))
    + kpi("Impacto en utilidad/mes", '<span style="color:'+col+'">'+(d>=0?'+':'')+usd0(d*SEM_MES)+'</span>', '×4.345 semanas')
    + '</div>'
    + (avisos?'<div style="margin-bottom:10px;display:flex;gap:6px;flex-wrap:wrap">'+avisos+'</div>':'')
    + (nAws?'<div class="sec-s" style="padding:0 2px 8px">Contexto forecast AWS: para '+nAws.toLocaleString()+' de estos modelos, AWS pronostica '+Math.round(awsT).toLocaleString()+' unidades el próximo mes (sin cambio de precio).</div>':'')
    + '<div class="card"><div class="sec-t">Los 20 más impactados (por |Δ utilidad|)</div>'
    + '<table><colgroup><col style="width:18%"><col style="width:26%"><col style="width:22%"><col style="width:16%"><col style="width:18%"></colgroup>'
    + '<thead><tr><th>Código</th><th>Proveedor</th><th>Precio</th><th>Unid/sem</th><th>Impacto en utilidad</th></tr></thead>'
    + '<tbody>'+filasM+'</tbody></table></div>'
    + '<div class="sec-s" style="padding:8px 2px">La banda IC95 suma los extremos por modelo (supone errores de ε correlacionados — conservador). '
    + 'El simulador no aplica pisos ni políticas: es una foto de elasticidad pura; el motor sí las aplica al recomendar.</div>';
}
function setDx(d){ dxAct = d;
  [["d_T","T"],["d_R","REACTIVAR"],["d_S","EVALUAR CONTINUIDAD"],["d_L","LIQUIDAR"]]
    .forEach(([id,val]) => document.getElementById(id).classList.toggle("btn-pri", val===dxAct));
  pintarD(); }
let cxAct = "T";
function setCx(c){ cxAct = c;
  if (c !== "T") setDx("REACTIVAR");
  ["T","LAB","REAL","PURA","SC"]
    .forEach(v => document.getElementById("c_"+v).classList.toggle("btn-pri", v===cxAct));
  pintarD(); }
const HIST_BADGE = {
  LAB:  '<span class="badge b-ambar" title="El costo subió pero NUNCA compramos a ese costo: el aumento fue teórico; el stock en mano conserva el costo viejo">🧪 LABORATORIO</span>',
  REAL: '<span class="badge b-gris" title="Hay compras posteriores al cambio: el costo nuevo es real">COSTO REAL</span>',
  PURA: '<span class="badge b-azul" title="La lista subió sin ningún cambio de costo en 24 meses">SUBIÓ SIN CAMBIO DE COSTO</span>',
  SC:   '<span class="badge b-gris" title="Hubo cambio de costo pero sin compra concluyente: el piso usa el costo del stock en mano">COSTO NUEVO SIN COMPRAS</span>'};
function celdaD(f){
  const [cod,dir,prov,ult,semSin,uEra,pEra,pHoy,delta,pSug,stock,cap,expl,hist,sidD,mesesS,remN,cmpD] = f;
  const badge = dir==="REACTIVAR" ? '<span class="badge b-verde">REACTIVAR</span>'
              : dir==="LIQUIDAR" ? '<span class="badge b-rojo">LIQUIDAR</span>'
              : dir==="SOLO PROYECTO" ? '<span class="badge b-gris">SOLO PROYECTO</span>'
              : dir==="REABASTECIDO: RE-EVALUAR" ? '<span class="badge b-azul" title="La pausa de venta coincidió con FALTA DE STOCK (no fue precio ni demanda); ya tiene stock de vuelta — re-evaluar como reinicio. Si la lista subió durante la pausa, ese precio casi no se ha probado">🔄 REABASTECIDO</span>'
              : dir==="VENDE POR PRESENTACIÓN" ? '<span class="badge b-verde" title="NO está dormido: se vende por carrete /KM (FiberHome); la venta individual = cantidad del carrete × km. El capital rota — cualquier decisión de precio va en el código del carrete">📦 VENDE POR /KM</span>'
              : dir==="ESPERAR STOCK" ? '<span class="badge b-azul" title="stock-first: al llegar la reposición se re-evalúa (en seguimiento diario)">⏳ ESPERAR STOCK</span>'
              : '<span class="badge b-ambar">EVALUAR CONTINUIDAD</span>';
  const remB = remN ? ' <span class="badge b-rojo" title="En REMATE'+(remN.startsWith("R")?" nivel "+remN:"")+' y dormido: rematarlo MÁS (peldaño +10% de profundidad, nunca revertir; considerar subir de nivel R en el canal)">🏷️ REMATE'+(remN.startsWith("R")?" "+remN:"")+'</span>' : "";
  const dtxt = delta===null ? "" :
    ' <span style="color:'+(delta>0?'var(--rojo)':'var(--gris)')+'">('+(delta>0?'+':'')+delta+'%)</span>';
  const lista = usd(pEra)+' → '+(pHoy===null?'—':usd(pHoy))+dtxt;
  // sin precio sugerido = DECISIÓN, no hueco (usuario 2026-07-31): el dormido
  // solo recibe precio cuando el precio es la enfermedad diagnosticada
  const SIN_PRECIO = {
    "LIQUIDAR": ["palanca: comercial", "Ya se bajó el precio y la venta NO respondió — otra bajada no es la respuesta. La palanca es comercial: remate, canal, empaque, bundle."],
    "EVALUAR CONTINUIDAD": ["palanca: surtido", "Murió SIN que la lista se moviera — no hay evidencia de que el precio sea el problema. La decisión es de surtido: ¿se sigue trabajando o se descontinúa?"],
    "REABASTECIDO: RE-EVALUAR": ["precio sin cambios: probar", "La lista no se movió durante la pausa de stock — no hay nada que corregir; darle semanas de prueba con stock antes de opinar."],
    "VENDE POR PRESENTACIÓN": ["precio: en el carrete", "El precio se decide en el código del carrete /KM, no en el individual."],
    "SOLO PROYECTO": ["precio: por trato", "Vende solo por proyecto — el precio se negocia por trato, la lista no es la palanca."],
  };
  const sp = SIN_PRECIO[dir] || ["sin palanca de precio", "El diagnóstico no es de precio."];
  let sug;
  if (pSug===null) {
    sug = '<span class="badge b-gris hint" title="'+sp[1]+'">'+sp[0]+'</span>';
  } else if (pHoy!==null && Math.abs(pSug-pHoy) < 0.01) {
    sug = '<b>'+usd(pSug)+'</b><span class="rng">MANTENER — con evidencia (ver explicación)</span>';
  } else if (dir==="REACTIVAR" || dir==="REABASTECIDO: RE-EVALUAR") {
    sug = '<b style="color:var(--azul)">'+usd(pSug)+'</b><span class="rng">su último precio con ventas</span>';
  } else if (pHoy!==null && pSug > pHoy*1.005) {
    sug = '<b style="color:var(--azul)">'+usd(pSug)+'</b><span class="rng">REVERTIR recorte que no revivió (ver explicación)</span>';
  } else {
    sug = '<b style="color:var(--azul)">'+usd(pSug)+'</b><span class="rng">peldaño de la cadencia — re-decisión cada 4 sem (ver explicación)</span>';
  }
  const clicD = sidD ? ' class="fila-sku" onclick="abrir(\\''+sidD+'\\')"' : "";
  let compB = '';
  if (cmpD){
    const [cm,cconf,cgap,csem,cmejor,cfte,cn] = cmpD;
    compB = cm === "E"
      ? ' <span class="badge b-rojo" title="⚔ La competencia vende ESTE MISMO modelo más barato: '+cfte.toUpperCase()+' a $'+cmejor.toLocaleString()+' USD ('+cgap+'% vs nuestra base) — posible explicación de la dormancia. Detalle en la pestaña ⚔ Competencia">⚔ COMP '+Math.abs(cgap)+'% ABAJO</span>'
      : ' <span class="badge b-gris" title="≈ La competencia vende un producto SIMILAR (otra marca, confiabilidad '+Math.round(cconf*100)+'%) más barato: '+cfte.toUpperCase()+' a $'+cmejor.toLocaleString()+' USD ('+cgap+'%). Contexto, no dato firme">≈ COMP SIMILAR '+Math.abs(cgap)+'% ABAJO</span>';
  }
  const chkD = (pSug !== null && pHoy !== null && Math.abs(pSug/pHoy-1) > 0.001)
    ? '<input type="checkbox" '+(selAplD.has(cod)?'checked ':'')+'data-c="'+cod+'" onclick="event.stopPropagation();tSelD(this.dataset.c,this)" style="margin-right:6px;vertical-align:middle" title="Seleccionar para aplicar el precio sugerido en el ERP"> ' : '';
  return '<tr'+clicD+'><td title="'+cod+'">'+chkD+(sidD?'▸ ':'')+'<span style="font-weight:600;color:var(--azul)">'+cod+'</span> '+badge+remB+compB
    +(hist && HIST_BADGE[hist] ? ' '+HIST_BADGE[hist] : '')+'</td>'
    +'<td title="'+(prov||"")+'"><span class="rng" style="font-size:12px;text-align:left">'+(prov||"—")+'</span></td>'
    +'<td class="num">'+ult+'<span class="rng" title="Semanas sin venta TENIENDO stock (reloj de muerte efectiva) — las semanas sin inventario no cuentan">'+semSin+' sem muerto c/stock</span></td>'
    +'<td class="num">'+Number(uEra).toLocaleString()+' u/sem</td>'
    +'<td class="num">'+lista+'</td><td class="num">'+sug+'</td>'
    +'<td class="num">'+(stock===null?'—':Number(stock).toLocaleString())+(mesesS!==null?' <span style="color:'+(mesesS>=12?'var(--rojo)':'var(--gris)')+';font-size:11px">('+mesesS+' m)</span>':'')+'<span class="rng">'+usd0(cap)+' atrapados'+(mesesS!==null?' · meses al ritmo de su época viva':'')+'</span></td>'
    +'<td style="text-align:left;font-size:11.5px;color:#374151" title="'+expl.replace(/"/g,"&quot;")+'"><div class="clamp3">'+expl+'</div></td></tr>';
}
function pintarD(){
  const q = document.getElementById("buscaD").value.trim().toUpperCase();
  const sel = FILAS2.filter(f => (dxAct==="T" || f[1]===dxAct)
    && (cxAct==="T" || f[13]===cxAct)
    && (!q || f[0].toUpperCase().includes(q)));
  document.getElementById("cuerpoD").innerHTML = sel.slice(0, 300).map(celdaD).join("");
  const capSel = sel.reduce((a,f)=>a+f[11],0);
  const filtrado = dxAct!=="T" || cxAct!=="T" || q;
  document.getElementById("contadorD").textContent = "— " + sel.length.toLocaleString()
    + " modelos" + (sel.length > 300 ? " (mostrando los 300 con más capital atrapado)" : "")
    + " · capital: " + usd0(capSel);
  // el monto del botón de la vista sigue a la SELECCIÓN (usuario 2026-08-01:
  // al elegir categoría, el $$ debe ser el de los modelos de esa categoría)
  document.getElementById("v_dorm").textContent = "💤 Dormidos (2ª capa) · "
    + sel.length.toLocaleString() + " · " + usd0(capSel) + " atrapados"
    + (filtrado ? " (selección)" : "");
}
pintar();
</script>"""
    js = (js.replace("__JS_LIB__", JS_LIB)
            .replace("__DATOS__", json.dumps(datos, ensure_ascii=False))
            .replace("__FILAS__", json.dumps(filas, ensure_ascii=False))
            .replace("__FILAS2__", json.dumps(filas2, ensure_ascii=False))
            .replace("__FILAS3__", json.dumps(filas3, ensure_ascii=False))
            .replace("__FILAS4__", json.dumps(filas4, ensure_ascii=False))
            .replace("__PROV4__", json.dumps(prov4, ensure_ascii=False))
            .replace("__CORTE__", str(corte))
            .replace("__CAL__", json.dumps(cal_js))
            .replace("__TIENE_PROV__", "true" if tiene_prov else "false")
            .replace("__PROVS__", json.dumps(provs, ensure_ascii=False))
            .replace("__SIM__", json.dumps(sim, ensure_ascii=False)))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(head + "".join(paneles) + js)
    print(f"reporte: {OUT} ({len(filas):,} modelos, {len(con_panel)} con panel embebido, "
          f"proveedor {'ACTIVO' if tiene_prov else 'pendiente censo'}, datos al {corte})", flush=True)


if __name__ == "__main__":
    generar(int(sys.argv[1]) if len(sys.argv) > 1 else 30,
            int(sys.argv[2]) if len(sys.argv) > 2 else 200)
