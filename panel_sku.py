# -*- coding: utf-8 -*-
"""Vista de detalle del panel para un SKU + bloques reutilizables.

Este módulo expone:
  - cargar_ctx()            : carga panel/recos/escenarios/eps una sola vez
  - cuerpo_sku(cod, ctx, ..): HTML del panel individual + datos de la gráfica
  - ESTILOS, JS_LIB         : CSS y render JS compartidos
  - generar(cod)            : archivo standalone out/panel_sku_<COD>.html

reporte_top.py reutiliza estos bloques para el resumen general clickeable.
Todo sale de las salidas del pipeline (ningún número a mano).

Uso standalone:  ./.venv/bin/python panel_sku.py LBEM523
"""
import json
import os
import re
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
Z95 = 1.96   # nivel de confianza 95% (usuario 2026-07-27; antes IC90/z=1.645)


def _fmt_sem(ts):
    t = pd.Timestamp(ts)
    return f"{t.day:02d} {MESES[t.month-1]} {str(t.year)[2:]}"


def _usd(x, dec=2):
    return f"${x:,.{dec}f}"


def _safe(codigo):
    return re.sub(r"[^A-Za-z0-9]", "_", str(codigo))


def _y_max(v_max):
    """Tope del eje Y: dato más alto del SKU +25%, redondeado hacia arriba a
    2 cifras significativas (para etiquetas de rejilla legibles)."""
    import math
    v = float(v_max) * 1.25
    if v <= 0:
        return 10
    sig = 3 if v >= 1000 else 2   # más resolución arriba de 1000 para no pasarse del +25%
    m = 10 ** max(0, int(math.floor(math.log10(v))) - (sig - 1))
    return int(math.ceil(v / m) * m)


def _chip_escenario(row):
    if row.escenario_pct == 0:
        return '<span class="badge b-gris">BASE</span>'
    if row.d_util_lo > 0:
        return '<span class="badge b-verde">SEGURA ✓</span>'
    if row.d_util_hi < 0:
        return '<span class="badge b-rojo">PIERDE</span>'
    return '<span class="badge b-ambar">DUDOSA</span>'


def _explica_escenario(x, eps_lo, eps_hi, eps_g):
    if x.escenario_pct == 0:
        return "Línea base: la venta y utilidad esperadas si no se toca el precio (u0 = pronóstico GBM, campeón del backtest)."
    eq = x.eps_equilibrio
    if x.escenario_pct > 0:
        if x.d_util_lo > 0:
            veces = abs(eq / eps_lo) if pd.notna(eq) else float("nan")
            return (f"Solo perdería dinero si la elasticidad real fuera ≤ {eq:.1f} — clientes "
                    f"{veces:.1f}× más sensibles que el PEOR caso medido ({eps_lo:.2f}). El margen extra "
                    f"por unidad cubre de sobra el volumen en riesgo: aún en el peor caso deja "
                    f"{x.d_util_lo:+,.0f}/sem.")
        if x.d_util_hi < 0:
            return (f"Pierde incluso con la elasticidad más favorable del rango medido ({eps_hi:.2f}): "
                    f"el margen extra no compensa el volumen perdido.")
        return (f"El punto de quiebre (elasticidad {eq:.1f}) cae DENTRO del rango medido con 95% de confianza "
                f"[{eps_lo:.2f}, {eps_hi:.2f}]: no se puede afirmar si gana o pierde. Regla: MANTENER.")
    else:
        if x.d_util_hi < 0:
            return (f"Para pagarse, cada 1% de descuento tendría que traer ≥{abs(eq):.1f}% más volumen "
                    f"(elasticidad ≤ {eq:.1f}); lo medido es ~{eps_g:.2f}, ni el extremo más elástico del rango "
                    f"({eps_lo:.2f}) se acerca. Aún en el mejor caso pierde {x.d_util_hi:+,.0f}/sem.")
        if x.d_util_lo > 0:
            return (f"La demanda medida es tan elástica que el volumen extra paga el descuento "
                    f"en todo el rango medido: mínimo {x.d_util_lo:+,.0f}/sem.")
        return (f"El punto de quiebre (elasticidad {eq:.1f}) cae dentro del rango medido: puede ir en "
                f"ambos sentidos. Regla: MANTENER.")


def cargar_ctx():
    ctx = {
        "pan": pd.read_parquet(os.path.join(BASE, "data", "panel.parquet")),
        "recos": pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv")),
        "esc": pd.read_csv(os.path.join(BASE, "out", "escenarios.csv")),
        "eps_g": pd.read_parquet(os.path.join(BASE, "data", "elasticidad_sku.parquet")).iloc[0],
    }
    # SELLO DE CORTE (auditoría 2026-07-30, C3): prohibido mezclar recos de un
    # corte con panel de otro — cubre a reporte_top y a los paneles sueltos.
    if "corte" in ctx["recos"].columns:
        c_recos = str(ctx["recos"].corte.iloc[0])
        c_pan = pd.Timestamp(ctx["pan"].semana.max()).date().isoformat()
        if c_recos != c_pan:
            raise SystemExit(f"SELLO DE CORTE: recomendaciones al {c_recos} pero panel al "
                             f"{c_pan} — re-corre escenarios.py (o run.py) antes de reportar")
    ruta = os.path.join(BASE, "data", "eps_por_sku.parquet")
    ctx["eps_map"] = pd.read_parquet(ruta).set_index("codigo") if os.path.exists(ruta) else None
    # pares del semáforo competitivo (para el bloque del detallado)
    ruta_sc = os.path.join(BASE, "data", "competencia", "semaforo.parquet")
    ctx["sem_par"] = pd.read_parquet(ruta_sc) if os.path.exists(ruta_sc) else None
    # registro del seguimiento diario de frenos (si existe)
    ruta_f = os.path.join(BASE, "out", "seguimiento_frenos.csv")
    ctx["frenos"] = (pd.read_csv(ruta_f).drop_duplicates("codigo").set_index("codigo")
                     if os.path.exists(ruta_f) else None)
    # revisión por costo (vigía diaria) + medianas del estudio de costos para
    # contextualizar el trade-off trasladar/absorber en el detallado
    ruta_rc = os.path.join(BASE, "out", "revision_costos.csv")
    ctx["rev_costos"] = (pd.read_csv(ruta_rc).drop_duplicates("codigo", keep="last")
                         .set_index("codigo") if os.path.exists(ruta_rc) else None)
    ruta_ac = os.path.join(BASE, "data", "analisis_costos.parquet")
    ctx["estudio_costos"] = None
    if os.path.exists(ruta_ac):
        ac = pd.read_parquet(ruta_ac)
        ac = ac[ac.u_pre >= 3]
        sub = ac[ac.d_costo_pct > 0]
        tras, abso = sub[sub.pass_through >= 0.5], sub[sub.pass_through.fillna(0) < 0.5]
        if len(tras) >= 50 and len(abso) >= 50:
            ctx["estudio_costos"] = {
                "tras_venta": float(tras.d_venta_pct.median()),
                "tras_marg": float(tras.d_margen_pts.median()),
                "abso_venta": float(abso.d_venta_pct.median()),
                "abso_marg": float(abso.d_margen_pts.median())}
    return ctx


def _tabla_meses(s, corte):
    """Tabla de ventas por MES: los últimos 11 meses cerrados + lo que va del
    mes actual (marcado *parcial). Unidades totales e ingreso neto."""
    d = s.copy()
    d["_mes"] = pd.to_datetime(d.semana).dt.to_period("M")
    d["_ing"] = d.unidades.astype(float) * d.neto_prom.astype(float)
    g = d.groupby("_mes").agg(u=("unidades", "sum"), ing=("_ing", "sum"))
    mes_actual = pd.Timestamp(corte).to_period("M")
    meses = pd.period_range(end=mes_actual, periods=12)
    primero = d._mes.min()
    ths, tds_u, tds_i = [], [], []
    for m in meses:
        lbl = f"{MESES[m.month-1]} {str(m.year)[2:]}"
        par = m == mes_actual
        ths.append(f'<th style="text-align:right">{lbl}{"*" if par else ""}</th>')
        if m < primero:
            tds_u.append('<td style="color:var(--gris-claro)">—</td>')
            tds_i.append('<td style="color:var(--gris-claro)">—</td>')
        else:
            u = g.u.get(m, 0.0)
            i = g.ing.get(m, 0.0)
            est = ' style="color:var(--gris)"' if par else ""
            tds_u.append(f'<td{est}>{u:,.0f}</td>')
            tds_i.append(f'<td{est}>{_usd(i,0)}</td>')
    return (f'<div class="card" style="margin-top:14px">'
            f'<div class="sec-t">Ventas por mes — últimos 12 meses</div>'
            f'<div class="sec-s">* {MESES[mes_actual.month-1]} {mes_actual.year} en curso '
            f'(parcial: datos al {pd.Timestamp(corte).date()})</div>'
            f'<div style="overflow-x:auto"><table class="num">'
            f'<tr><th style="text-align:left">Mes</th>{"".join(ths)}</tr>'
            f'<tr><td style="text-align:left"><b>Unidades</b></td>{"".join(tds_u)}</tr>'
            f'<tr><td style="text-align:left"><b>Ingreso neto</b></td>{"".join(tds_i)}</tr>'
            f'</table></div></div>')


def cuerpo_sku(codigo, ctx, volver_html=""):
    """HTML del panel individual + dict de datos para la gráfica (renderSku)."""
    pan, recos, esc = ctx["pan"], ctx["recos"], ctx["esc"]
    s = pan[pan.codigo == codigo].sort_values("semana")
    rr = recos[recos.codigo == codigo]
    if s.empty or rr.empty:
        raise SystemExit(f"{codigo}: sin datos en panel/recomendaciones")
    r = rr.iloc[0]
    e = esc[esc.codigo == codigo].sort_values("escenario_pct")
    sid = _safe(codigo)

    total_sem = pan.semana.nunique()
    semanas = [_fmt_sem(w) for w in s.semana]
    unidades = [int(x) for x in s.unidades]
    listas = [round(float(x), 2) for x in s.precio_lista]
    fecha_corte = pd.Timestamp(pan.semana.max()).date()

    # ε del SKU (su segmento)
    e_c, e_se, seg = float(ctx["eps_g"].eps_global), float(ctx["eps_g"].se_global), "global"
    if ctx["eps_map"] is not None and codigo in ctx["eps_map"].index:
        m = ctx["eps_map"].loc[codigo]
        e_c, e_se, seg = float(m.eps), float(m.se), str(m.segmento)
        if "nivel" in m.index and "capa SKU" in str(m.nivel):
            seg = f"{seg}, afinado con SUS propios cambios de precio"
        elif "nivel" in m.index and "proveedor" in str(m.nivel):
            seg = f"{seg}, afinado por proveedor"
    e_lo, e_hi = e_c - Z95 * e_se, e_c + Z95 * e_se
    # comparativo de 3 niveles con las ε VIGENTES del modelo (auditoría K2:
    # nunca hardcodear — se re-estiman cada corrida)
    comp3 = "1 poco sensible · 2 media · 3 muy sensible"
    if ctx["eps_map"] is not None and "nivel" in ctx["eps_map"].columns:
        _s = (ctx["eps_map"][ctx["eps_map"].nivel == "segmento"]
              .groupby("segmento").eps.first())
        if {"bajo", "medio", "alto"}.issubset(_s.index):
            comp3 = (f"1 poco sensible ({_s['bajo']:.2f}) · 2 media ({_s['medio']:.2f}) · "
                     f"3 muy sensible ({_s['alto']:.2f})")

    # cambios de lista (>0.5%); etiquetar los 2 más grandes
    cambios = []
    for i in range(1, len(listas)):
        if listas[i - 1] > 0 and abs(listas[i] / listas[i - 1] - 1) > 0.005:
            cambios.append({"i": i, "antes": listas[i - 1], "despues": listas[i],
                            "pct": 100 * (listas[i] / listas[i - 1] - 1), "sem": semanas[i]})
    for c in sorted(cambios, key=lambda c: -abs(c["pct"]))[:2]:
        c["lbl"] = f"{_usd(c['antes'])} → {_usd(c['despues'])} ({c['pct']:+.1f}%)"

    rho = (s.neto_prom / s.precio_lista)
    dir_txt = {"SUBIR": f"SUBIR +{r.cambio_pct:.0f}%", "BAJAR": f"BAJAR {r.cambio_pct:.0f}%",
               "MANTENER": "MANTENER"}[r.direccion]
    dir_cls = "b-verde" if r.direccion == "SUBIR" else ("b-rojo" if r.direccion == "BAJAR" else "b-gris")
    conf_cls = {"alta": "b-azul", "media": "b-ambar", "baja": "b-gris"}[r.confianza]
    du = r.utilidad_sem_sugerido - r.utilidad_sem_mantener

    exp_cambios = ""
    if cambios:
        ult = cambios[-1]
        antes_u = np.mean(unidades[max(0, ult["i"] - 8):ult["i"]])
        despues_u = np.mean(unidades[ult["i"]:])
        exp_cambios = (f"<li><b>Último aumento observado:</b> {ult['pct']:+.1f}% ({ult['sem']}); "
                       f"volumen pasó de {antes_u:,.0f} a {despues_u:,.0f} u/sem</li>")

    motivo_rev = str(r.get("revisar", "") or "")
    if motivo_rev and "frenar" in motivo_rev:
        # FRENO por reabasto: subida táctica ACTIVA (no "en revisión"), con
        # seguimiento diario hasta que llegue la reposición
        badge_rev = f'<span class="badge b-azul" title="{motivo_rev}">⏸ FRENO POR REABASTO</span>'
        frenos_reg = ctx.get("frenos")
        if frenos_reg is not None and codigo in frenos_reg.index:
            st = frenos_reg.loc[codigo]
            if str(st.estado) == "esperando_reposicion":
                badge_rev += (' <span class="badge b-gris" title="cron diario 8:30 L-V: '
                              'alerta cuando la cobertura recupere ≥6 semanas">'
                              'EN SEGUIMIENTO DIARIO · esperando reposición</span>')
            elif str(st.estado) == "reabastecido":
                badge_rev += (f' <span class="badge b-rojo">🔔 REABASTECIDO — RE-DECIDIR: '
                              f'{str(st.recomendacion)}</span>')
    elif motivo_rev and motivo_rev != "nan":
        badge_rev = f'<span class="badge b-ambar">EN REVISIÓN: {motivo_rev.upper()}</span>'
    else:
        badge_rev = ""
    exist_li = ""
    disp = r.get("disponible", r.get("existencia"))
    if pd.notna(disp) and pd.notna(r.get("meses_inv")):
        tono = ' style="color:var(--rojo)"' if r.meses_inv >= 12 else ""
        repo = r.get("reposicion")
        repo_txt = f" · reposición en camino: {repo:,.0f} uds" if pd.notna(repo) and repo > 0 else ""
        fte = str(r.get("meses_fuente", "") or "")
        exist_li = (f"<li><b>Stock en almacenes de venta:</b> {disp:,.0f} uds (sin apartada; "
                    f"<span{tono}>{r.meses_inv:.1f} meses de stock</span> vs demanda esperada"
                    + (f" — fuente: <b>{fte}</b>" if fte else "")
                    + ("; guardrail sobrestock ≥12m activo" if r.meses_inv >= 12 else "")
                    + f"){repo_txt}</li>")

    # demanda y territorio, EXPLÍCITOS (los chips del resumen solo resumen esto)
    dem_li = ""
    crec = r.get("crecimiento")
    if pd.notna(crec) and abs(crec) >= 0.03:
        racha = str(r.get("meses_alza") or "")
        dem_li += (f"<li><b>Demanda {'creciendo' if crec > 0 else 'cayendo'} "
                   f"{100*crec:+.0f}%/mes</b> (cambio mediano mes vs mes, últimos 6 meses "
                   f"cerrados{f'; {racha} meses al alza' if racha and racha != 'nan' else ''})</li>")
    act = r.get("aws_suc_activas")
    if pd.notna(act):
        frac, top_sh = r.get("aws_suc_alza_frac"), r.get("aws_suc_top_share")
        n_up = int(round(float(frac) * act)) if pd.notna(frac) else None
        partes = [f"venta reciente en <b>{act:.0f} sucursal{'es' if act > 1 else ''}</b>"]
        if pd.notna(top_sh) and act > 1:
            partes.append(f"la mayor concentra <b>{100*top_sh:.0f}%</b>")
        if n_up is not None:
            partes.append(f"el forecast AWS prevé alza en <b>{n_up} de {act:.0f}</b>")
        lectura = ""
        if act == 1 or (pd.notna(top_sh) and top_sh >= 0.6):
            lectura = (" — 📍 venta concentrada: puede ser cliente/proyecto local, "
                       "no mercado general (el precio es nacional; leer con cautela)")
        elif n_up is not None and act >= 3 and n_up / act >= 0.5:
            lectura = " — 🗺️ demanda ancha: el movimiento nacional tiene soporte territorial"
        dem_li += f"<li><b>Territorio:</b> {'; '.join(partes)}{lectura}</li>"
    # escaparate web (API de BI, informativo): vistas → compras y conversión
    # comparada contra la mediana del sitio. Serie joven (nació 2026-07-23).
    vis = r.get("web_vistas")
    if pd.notna(vis) and vis >= 20:
        conv, cmed = r.get("web_conv_pct"), r.get("web_conv_med")
        comp = ""
        if pd.notna(conv) and pd.notna(cmed) and cmed > 0:
            if conv >= 2 * cmed:
                comp = (f" — 🛒 convierte MUY BIEN (mediana del sitio {cmed:.0f}%): "
                        "poder de precio observado en la vitrina")
            elif conv <= cmed / 2:
                comp = (f" — 👁 miran mucho, compran poco (mediana del sitio {cmed:.0f}%): "
                        "hay demanda que no se concreta")
            else:
                comp = f" (mediana del sitio: {cmed:.0f}%)"
        dem_li += (f"<li><b>Escaparate web</b> (últimos {r.web_dias:.0f} días, informativo): "
                   f"<b>{vis:,.0f} vistas → {r.web_compras:,.0f} compras</b> = conversión "
                   f"<b>{conv:.1f}%</b>{comp}</li>")

    # MEZCLA DE CANAL — solo cuando fue DETERMINANTE (usuario 2026-08-01: sin
    # chip en el resumen; aquí, en el detallado, la explicación completa)
    cj = str(getattr(r, "canal_ajuste", "") or "")
    if cj and cj != "nan":
        pl = float(getattr(r, "pct_linea", np.nan))
        if cj == "media→alta":
            dem_li += (f"<li><b>Canal de venta (regla 2026-08-01):</b> el "
                       f"<b>{100*pl:.0f}%</b> de su venta (26 sem) es EN LÍNEA — el cliente "
                       f"compra solo, sin ejecutivo que amortigüe el alza. El estudio de "
                       f"9,805 alzas pareadas mide retención <b>0.936</b> en este canal para "
                       f"alzas de 2-5% (vs 0.868 por vendedor) y el aumento aterriza completo "
                       f"en el neto (pass-through 1.03). Por eso la confianza de esta "
                       f"sugerencia subió de media a <b>alta</b>.</li>")
        else:
            dem_li += (f"<li><b>Canal de venta (regla 2026-08-01):</b> solo el "
                       f"<b>{100*pl:.0f}%</b> de su venta es en línea — domina el canal con "
                       f"vendedor, donde las alzas rinden menos de lo proyectado (ε efectiva "
                       f"−3.04 vs −2.07 en línea) y el vendedor puede amortiguar con descuento "
                       f"extra (d1&gt;20): el neto no subiría lo que la lista. Por eso la "
                       f"confianza de esta sugerencia bajó de alta a <b>media</b>.</li>")

    # VENTANA DE 5 ESCENARIOS centrada en el recomendado (usuario 2026-07-27):
    # el sugerido + 2 menores + 2 mayores. La rejilla completa sigue en
    # out/escenarios.csv; aquí solo se muestra la vecindad de la decisión.
    e = e.reset_index(drop=True)
    pcts = e.escenario_pct.tolist()
    i_reco = pcts.index(r.cambio_pct) if r.cambio_pct in pcts else pcts.index(0)
    ini = max(0, min(i_reco - 2, len(e) - 5))
    e = e.iloc[ini:ini + 5]

    filas_esc = []
    for _, x in e.iterrows():
        nombre = ("Mantener" if x.escenario_pct == 0 else
                  ("Subir" if x.escenario_pct > 0 else "Bajar") + f" {abs(x.escenario_pct):.0f}%")
        # el sugerido se resalta SIEMPRE, incluido MANTENER (usuario 2026-07-30):
        # no mover el precio también es una decisión del motor
        sug = x.escenario_pct == r.cambio_pct
        guard = ' <span style="color:var(--gris-claro)">(fuera de guardrail)</span>' if abs(x.escenario_pct) > 4 else ""
        d_txt = ("$0<span class=\"rng\">línea base</span>" if x.escenario_pct == 0 else
                 f"<span style=\"color:var(--{'verde' if x.d_util_vs_mantener>0 else 'rojo'})\">"
                 f"{x.d_util_vs_mantener:+,.0f}</span>"
                 f"<span class=\"rng\">con 95% de conf.: {x.d_util_lo:+,.0f} a {x.d_util_hi:+,.0f}</span>")
        cls = ' class="row-sug"' if sug else ""
        cls_exp = ' class="row-sug row-sug-exp"' if sug else ""
        filas_esc.append(
            f'<tr{cls}><td>{nombre}'
            + (' · <span class="badge" style="background:var(--azul);color:#fff">★ SUGERIDO</span>' if sug else guard)
            + f'</td><td>{_usd(x.precio)}</td>'
            f'<td>{x.unidades_sem:,.0f}<span class="rng">rango {x.unidades_p10:,.0f}–{x.unidades_p90:,.0f}</span></td>'
            f'<td>{d_txt}</td><td>{_chip_escenario(x)}</td></tr>'
            f'<tr{cls_exp}><td colspan="5" class="expl">{_explica_escenario(x, e_lo, e_hi, e_c)}</td></tr>')

    # NOTA DE POLÍTICA en escenarios (ronda 3, M2): si la decisión está
    # bloqueada por remate o ancla de canasta, la tabla sigue mostrando las
    # ganancias "en papel" de subir/bajar — aclarar que NO están disponibles
    aviso_politica = ""
    mot_ = str(r.revisar or "")
    if "remate" in mot_:
        aviso_politica = ('<div class="sec-s" style="color:var(--rojo)">🏷️ Este modelo está '
                          'en REMATE: dejar agotar el stock. Las ganancias de subir/bajar de '
                          'esta tabla son teóricas — el precio lo gobierna el canal de remate.</div>')
    elif "ancla" in mot_:
        aviso_politica = ('<div class="sec-s" style="color:var(--rojo)">⚓ SUBIR está BLOQUEADO '
                          'por ancla de canasta: la ganancia propia que muestra esta tabla NO '
                          'descuenta el arrastre en sus compañeros de folio (ver motivo de la '
                          'decisión) — subir destruiría más margen del que gana.</div>')

    # bloque de MOVIMIENTO DE COSTO (usuario 2026-07-31: el detallado individual
    # acompaña a cada "💲 Costo movió") — datos de la vigía + guía empírica del
    # estudio de 21K eventos (trade-off trasladar/absorber)
    # BLOQUE DE COMPETENCIA (usuario 2026-08-10): análisis detallado de todos
    # los pares del modelo — quién, a cuánto, y las 4 firmas del semáforo
    bloque_comp = ""
    sp = ctx.get("sem_par")
    if sp is not None:
        pares_c = sp[sp.codigo == codigo].sort_values("precio_comp_usd")
        if len(pares_c):
            SEMTXT = {"AMENAZA REAL": ("🔴 AMENAZA REAL", "var(--rojo)"),
                      "REMATE AJENO": ("🟠 REMATE AJENO", "#b45309"),
                      "ESPACIO": ("🟢 ESPACIO", "var(--verde)"),
                      "NEUTRO": ("NEUTRO", "var(--gris)")}
            fr = []
            for p_ in pares_c.itertuples():
                et, colr = SEMTXT[p_.semaforo]
                m_ = ("100%" if p_.match == "EXACTO"
                      else f"similar {p_.confiabilidad:.0%}")
                fr.append(
                    f'<tr><td>{p_.fuente.upper()}<span class="rng">{str(p_.modelo_comp)[:24]}</span></td>'
                    f'<td>{m_}</td><td class="num">${p_.precio_comp_usd:,.2f}</td>'
                    f'<td class="num" style="color:var(--{"rojo" if p_.gap_pct < 0 else "verde"})">{p_.gap_pct:+.1f}%</td>'
                    f'<td class="num">{int(p_.persistencia_d) if pd.notna(p_.persistencia_d) else "—"}d</td>'
                    f'<td>{p_.stock_comp}</td><td style="color:{colr};font-weight:600">{et}</td></tr>')
            bloque_comp = (
                '<div class="card" style="margin-top:14px">'
                '<div class="sec-t">⚔ Competencia — análisis detallado</div>'
                '<div class="sec-s">Cada fila es un competidor real vendiendo este modelo (match 100%) o su '
                'equivalente de otra marca (similar, con su confiabilidad). El semáforo pondera 4 firmas: '
                'persistencia del precio (≥7 días = posición real), la trayectoria de SU stock '
                '(VENDIENDO = rota de verdad; ESTANCADO = atorado, su precio no es mercado), consenso entre '
                'fuentes, y NUESTRO daño (venta cayendo). Solo la AMENAZA REAL amerita defensa; el remate '
                'ajeno se ignora.</div>'
                '<table class="num"><tr><th>Competidor</th><th>Match</th><th>Su precio</th>'
                '<th>Gap vs subtotal</th><th title="días del precio al nivel actual">Persist.</th>'
                '<th>Su stock</th><th>Semáforo</th></tr>'
                + "".join(fr) + '</table></div>')

    bloque_costo = ""
    rc = ctx.get("rev_costos")
    if rc is not None and codigo in rc.index:
        ev = rc.loc[codigo]
        if (pd.Timestamp.today().normalize() - pd.Timestamp(ev.fecha_ultimo)).days <= 21:
            up = float(ev.pct) > 0
            marg_txt = ""
            if pd.notna(ev.margen_antes) and pd.notna(ev.margen_nuevo):
                marg_txt = (f" · margen estimado al precio actual: "
                            f"<b>{100*ev.margen_antes:.1f}% → {100*ev.margen_nuevo:.1f}%</b>")
            est = ctx.get("estudio_costos")
            if up and est:
                guia = (f"Guía empírica (21K movimientos de costo, 2024→hoy): "
                        f"<b>trasladar</b> una subida costó {est['tras_venta']:+.1f}% de venta "
                        f"pero protegió el margen ({est['tras_marg']:+.1f} pts); "
                        f"<b>absorberla</b> protegió la venta ({est['abso_venta']:+.1f}%) pero "
                        f"costó {est['abso_marg']:+.1f} pts de margen. Si el costo nuevo rompe "
                        f"el piso, la defensa de margen ya alertó con la lista mínima.")
            elif up:
                guia = ("Revisar precio: el margen se comprime; si rompe el piso costo+3pts, "
                        "la defensa de margen ya alertó con la lista mínima.")
            else:
                guia = ("Oportunidad: el costo bajó — absorber captura margen; trasladar "
                        "compra volumen. El motor re-decide con el costo nuevo al cierre "
                        "del ciclo.")
            bloque_costo = f"""
  <div class="card" style="border-left:4px solid var(--{'rojo' if up else 'azul'})">
    <div class="sec-t">{'🔺' if up else '🔻'} Movimiento de costo — dispara revisión de precio (vigía diaria)</div>
    <div class="sec-s">detectado {ev.fecha_detectado}{' · último movimiento ' + str(ev.fecha_ultimo) if str(ev.fecha_ultimo) != str(ev.fecha_detectado) else ''}</div>
    <div style="padding:2px 14px 12px;font-size:13px">
      Costo del proveedor: <b class="num">{_usd(float(ev.costo_base))} → {_usd(float(ev.costo_hoy))}
      ({float(ev.pct):+.1f}%)</b>{marg_txt}<br>
      <span style="color:var(--gris)">{guia}</span>
    </div>
  </div>"""

    boton = (f'<button class="btn btn-pri" onclick="aplicarUno(\'{codigo}\', {r.precio_sugerido})">Aplicar {_usd(r.precio_sugerido)}</button>'
             if r.direccion != "MANTENER" else
             '<button class="btn" disabled>Sin acción este mes</button>')

    row_s = e[e.escenario_pct == r.cambio_pct]
    proy = float(row_s.unidades_sem.iloc[0]) if len(row_s) else r.u_sem_proyectado

    D = {"id": sid, "cid": f"chart_{sid}", "sid_": f"pstrip_{sid}",
         "sem": semanas, "u": unidades, "p": listas, "cambios": cambios,
         # eje Y ceñido al modelo (usuario 2026-07-30): tope = dato más alto
         # del propio SKU +25%, redondeado a 2 cifras — antes se redondeaba a
         # múltiplos de 500 y aplastaba las variaciones de los SKUs chicos
         "yMax": _y_max(float(max(r.u_sem_p90, max(unidades)))),
         "nProy": 3, "proy": round(proy, 0), "mant": round(float(r.u_sem_actual), 0),
         "bLo": float(r.u_sem_p10), "bHi": float(r.u_sem_p90),
         "pSug": float(r.precio_sugerido), "dir": r.direccion}

    html = f"""
  <div class="top">
    {volver_html}
    <span style="font-size:20px;font-weight:700;color:var(--azul)">{codigo}</span>
    <span class="badge {conf_cls}">CONFIANZA {r.confianza.upper()}</span>
    <span class="badge {dir_cls}">{dir_txt}</span>
    {badge_rev}
    <span style="font-size:12px;color:var(--gris)">lista tipo {int(r.tipo_precio)} · datos al {fecha_corte}</span>
    <span style="flex:1"></span>
    <button class="btn">Posponer</button>
    {boton}
  </div>

  <div class="kpis">
    <div class="card kpi"><div class="lbl">Precio actual</div><div class="val num">{_usd(r.precio_actual)}</div><div class="sub">lista vigente</div></div>
    <div class="card kpi"><div class="lbl">Precio sugerido</div><div class="val num" style="color:var(--azul)">{_usd(r.precio_sugerido)}</div><div class="sub">paso {r.cambio_pct:+.0f}% · ciclo de 3 semanas</div></div>
    <div class="card kpi"><div class="lbl">Venta esperada</div><div class="val num">{r.u_sem_actual:,.0f}<span style="font-size:12px;font-weight:400;color:var(--gris)"> u/sem</span></div><div class="sub">pronóstico del modelo, validado contra la realidad</div></div>
    <div class="card kpi"><div class="lbl">Margen neto</div><div class="val num">{100*r.margen_actual:.0f}%</div><div class="sub">sobre precio neto cobrado</div></div>
    <div class="card kpi"><div class="lbl">Utilidad</div><div class="val num">{_usd(r.utilidad_sem_mantener,0)}<span style="font-size:12px;font-weight:400;color:var(--gris)">/sem</span></div><div class="sub">al precio actual</div></div>
  </div>

  <div class="card">
    <div class="sec-t">Ventas semanales y precio de lista — {len(s)} semanas + proyección del ciclo (3 sem)</div>
    <div class="sec-s">{len(cambios)} cambio(s) de lista en la ventana · pass-through neto/lista: {rho.median():.3f} (σ {rho.std():.3f})</div>
    <div style="padding:0 8px 4px"><svg id="chart_{sid}" width="100%" height="300" viewBox="0 0 1040 300" role="img" aria-label="Unidades por semana"></svg></div>
    <div style="padding:0 8px 10px"><svg id="pstrip_{sid}" width="100%" height="64" viewBox="0 0 1040 64" role="img" aria-label="Precio de lista por semana"></svg></div>
  </div>

  {bloque_comp}

  {bloque_costo}

  {_tabla_meses(s, pan.semana.max())}

  <div class="grid2">
    <div class="card">
      <div class="sec-t">Escenarios de precio — el sugerido y sus vecinos (±2 pasos)</div>
      {aviso_politica}
      <div class="sec-s">Unidades: margen de error real del pronóstico. Utilidad extra: rango con 95% de confianza por la
        incertidumbre de <span class="hint" title="Sensibilidad al precio de este modelo: elasticidad {e_c:.2f} — si el precio sube 1%, su venta baja ~{abs(e_c):.1f}%. Comparativo de los 3 niveles del catálogo (por rotación): {comp3}. Este modelo: segmento {seg}.">la elasticidad ({e_c:.2f} ±{Z95*e_se:.2f}, segmento {seg})</span>; la <b>factibilidad de la decisión</b> la gobierna la elasticidad.
        Rejilla completa ±10% en out/escenarios.csv</div>
      <table class="num">
        <tr><th>Escenario</th><th>Precio</th><th>Unid/sem</th><th>Utilidad extra vs no mover</th><th>Decisión</th></tr>
        {''.join(filas_esc)}
      </table>
      <div style="padding:8px 14px 12px;font-size:11px;color:var(--gris)">
        <b>SEGURA ✓</b> = gana en todo el rango medido con 95% de confianza. <b>DUDOSA</b> = puede ganar o perder.
        <b>PIERDE</b> = pierde en todo el rango.
      </div>
    </div>

    <div class="card">
      <div class="sec-t">Qué respalda esta recomendación</div>
      <div class="ev"><ul style="margin:6px 0;padding-left:18px">
        <li><b>{len(s)}/{total_sem} semanas</b> con venta · <b>~{s.n_clientes.mean():,.0f} clientes/sem</b></li>
        <li><b>{len(cambios)} cambios de lista</b> observados (variación real para estimar respuesta)</li>
        {exp_cambios}
        {exist_li}
        {dem_li}
        <li><b>Pass-through estable:</b> neto/lista = {rho.median():.3f} — el neto sigue a la lista</li>
        <li><b>Rango honesto:</b> unidades del ciclo entre <b class="num">{r.u_sem_p10:,.0f} y {r.u_sem_p90:,.0f}</b>/sem</li>
        <li>Al aplicar: <b class="num">{r.u_sem_proyectado:,.0f} u/sem</b> y utilidad <b style="color:var(--verde)" class="num">{du:+,.0f}/sem</b></li>
      </ul></div>
      <div style="margin:0 14px 14px;padding:10px 12px;background:var(--fondo);border-radius:6px;font-size:12px;color:var(--gris)">
        <b style="color:var(--tinta)">Seguimiento:</b> si se aplica, la proyección queda registrada y se contrasta
        contra la venta real al cierre del ciclo (3 semanas).
      </div>
    </div>
  </div>
"""
    return html, D


ESTILOS = """<style>
  :root{
    --azul:#2563eb; --azul-suave:#eff6ff; --tinta:#1f2937; --gris:#6b7280; --gris-claro:#9ca3af;
    --borde:#e5e7eb; --fondo:#f6f7f9; --blanco:#ffffff;
    --verde:#067647; --verde-bg:#ecfdf3; --rojo:#b42318;
  }
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       color:var(--tinta);background:var(--fondo);margin:0;font-size:14px}
  .wrap{max-width:1080px;margin:0 auto;padding:20px 16px}
  .card{background:var(--blanco);border:1px solid var(--borde);border-radius:8px}
  .num{font-variant-numeric:tabular-nums}
  a{color:var(--azul);text-decoration:none}
  .top{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
  .badge{font-size:10px;font-weight:700;border-radius:4px;padding:2px 7px;letter-spacing:.4px}
  .b-azul{color:var(--azul);background:var(--azul-suave)}
  .b-verde{color:var(--verde);background:var(--verde-bg)}
  .b-rojo{color:var(--rojo);background:#fef3f2}
  .b-gris{color:var(--gris);background:var(--fondo)}
  .b-ambar{color:#b45309;background:#fffbeb}
  .rng{display:block;font-size:10px;color:var(--gris);font-weight:400}
  .hint{border-bottom:1px dotted var(--gris);cursor:help}
  .btn{border-radius:6px;padding:8px 16px;font-size:13px;font-weight:600;border:1px solid var(--borde);
       background:var(--blanco);color:var(--tinta);cursor:pointer}
  .btn-pri{background:var(--azul);border-color:var(--azul);color:#fff}
  .btn:focus-visible{outline:2px solid var(--azul);outline-offset:2px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
  .kpi{padding:12px 14px}
  .kpi .lbl{font-size:10px;font-weight:600;color:var(--gris);text-transform:uppercase;letter-spacing:.4px}
  .kpi .val{font-size:20px;font-weight:700;margin-top:2px}
  .kpi .sub{font-size:11px;color:var(--gris);margin-top:2px}
  .grid2{display:grid;grid-template-columns:1.7fr 1fr;gap:14px;margin-top:14px}
  @media(max-width:860px){.grid2{grid-template-columns:1fr}}
  .sec-t{font-size:12px;font-weight:700;padding:12px 14px 0}
  .sec-s{font-size:11px;color:var(--gris);padding:2px 14px 8px;line-height:1.5}
  table{border-collapse:collapse;width:100%}
  th{padding:8px 12px;font-size:10px;font-weight:600;color:var(--gris);text-transform:uppercase;
     letter-spacing:.3px;border-bottom:1px solid var(--borde);background:var(--fondo);text-align:right}
  th:first-child{text-align:left}
  td{padding:8px 12px;font-size:13px;border-bottom:1px solid var(--borde);text-align:right}
  td:first-child{text-align:left}
  tr:last-child td{border-bottom:none}
  .row-sug{background:var(--azul-suave)}
  .row-sug td{font-weight:700;border-top:2px solid var(--azul)}
  .row-sug td:first-child{border-left:3px solid var(--azul)}
  .row-sug td:last-child{border-right:3px solid var(--azul)}
  .row-sug-exp td{border-top:none;border-bottom:2px solid var(--azul);font-weight:400}
  .row-sug-exp td:first-child{border-left:3px solid var(--azul)}
  .row-sug-exp td:last-child{border-right:3px solid var(--azul)}
  .expl{text-align:left !important;font-size:11.5px !important;color:var(--gris) !important;
        font-weight:400 !important;padding:0 12px 10px 24px !important;border-top:none;line-height:1.5}
  tr:has(+ tr .expl) td{border-bottom:none}
  .ev{padding:4px 14px 12px;font-size:13px;line-height:1.55;color:#374151}
  .ev li{margin-bottom:6px}
  .tip{position:fixed;pointer-events:none;background:var(--tinta);color:#fff;font-size:11px;
       padding:6px 9px;border-radius:6px;opacity:0;transition:opacity .12s;z-index:9;white-space:nowrap}
  .foot{font-size:11px;color:var(--gris);margin-top:14px;line-height:1.6}
  .oculto{display:none}
  .fila-sku{cursor:pointer}
  .fila-sku:hover{background:var(--azul-suave)}
</style>"""

# Render de gráfica + franja de precio para un SKU (datos en D). Idempotente.
def cuerpo_dormido(codigo, ctx, fila, exist=None, volver_html=""):
    """Panel de detalle para un DORMIDO (2ª capa): historia completa de venta,
    película de costos de 24m y explicación del estatus. `fila` = su renglón de
    segunda_capa_dormidos.csv. Reutiliza renderSku (proyección en 0: un dormido
    que se mantiene igual proyecta cero venta)."""
    pan = ctx["pan"]
    s = pan[pan.codigo == codigo].sort_values("semana")
    if s.empty:
        raise ValueError(f"{codigo}: sin historia en panel")
    sid = _safe(codigo)
    semanas = [_fmt_sem(w) for w in s.semana]
    unidades = [int(x) for x in s.unidades]
    listas = [round(float(x), 2) for x in s.precio_lista]
    cambios = []
    for i in range(1, len(listas)):
        if listas[i - 1] > 0 and abs(listas[i] / listas[i - 1] - 1) > 0.005:
            cambios.append({"i": i, "antes": listas[i - 1], "despues": listas[i],
                            "pct": 100 * (listas[i] / listas[i - 1] - 1), "sem": semanas[i]})
    for c in sorted(cambios, key=lambda c: -abs(c["pct"]))[:2]:
        c["lbl"] = f"{_usd(c['antes'])} → {_usd(c['despues'])} ({c['pct']:+.1f}%)"

    x = fila
    dir_cls = {"REACTIVAR": "b-verde", "LIQUIDAR": "b-rojo",
               "ESPERAR STOCK": "b-azul"}.get(str(x.direccion), "b-ambar")
    hist_badge = ""
    if x.direccion == "REACTIVAR":
        if x.get("incremento_laboratorio") is True or x.get("incremento_laboratorio") == True:
            hist_badge = ('<span class="badge b-ambar">🧪 INCREMENTO DE LABORATORIO</span>')
        elif x.get("compramos_tras_cambio") == True:
            hist_badge = '<span class="badge b-gris">COSTO REAL (sí compramos)</span>'

    # película de costos: cambios ≥2% de costo_prov en la ventana
    filas_costo = ""
    if exist is not None:
        se = exist[exist.codigo == codigo].sort_values("semana")
        if "costo_prov" in se.columns and se.costo_prov.notna().any():
            cp = se.set_index("semana").costo_prov.dropna()
            prev, eventos = None, []
            for f_, v in cp.items():
                if prev and abs(v / prev - 1) >= 0.02:
                    eventos.append((f_, prev, v))
                prev = v if v > 0 else prev
            for f_, a, b in eventos[-8:]:
                col = "var(--rojo)" if b > a else "var(--verde)"
                filas_costo += (f"<tr><td>{_fmt_sem(f_)}</td><td class='num'>{_usd(a)}</td>"
                                f"<td class='num' style='color:{col}'>{_usd(b)} "
                                f"({100*(b/a-1):+.1f}%)</td></tr>")
    if not filas_costo:
        filas_costo = ("<tr><td colspan='3' style='color:var(--gris)'>sin cambios de "
                       "costo ≥2% registrados en la ventana</td></tr>")
    dos_costos = ""
    if pd.notna(x.get("costo_stock_mano")) and pd.notna(x.get("costo_prov_hoy")):
        dos_costos = (f"<li><b>Los dos costos:</b> stock en mano {_usd(x.costo_stock_mano)} "
                      f"vs reposición hoy {_usd(x.costo_prov_hoy)} — el piso de reactivación "
                      f"respeta el del stock EN MANO (regla F2)</li>")

    p_hoy = float(x.precio_hoy) if pd.notna(x.precio_hoy) else float(listas[-1])
    delta_txt = (f" ({x.delta_precio_pct:+.1f}%)" if pd.notna(x.delta_precio_pct) else "")
    sug = (_usd(float(x.precio_sugerido)) if pd.notna(x.precio_sugerido) else "—")

    # ---- ¿POR QUÉ ESTÁ CLASIFICADO ASÍ? — factores evaluados, con sus números
    def _f(icono, titulo, detalle):
        return (f'<li style="margin-bottom:8px"><span style="font-size:15px">{icono}</span> '
                f'<b>{titulo}</b><br><span style="color:var(--gris);font-size:12.5px">{detalle}</span></li>')
    factores = [
        _f("1️⃣", f"Dejó de vender hace {int(x.semanas_sin_venta)} semanas",
           f"última venta: {x.ultima_venta}. El motor principal solo evalúa modelos con venta "
           f"reciente — por eso pasó a la 2ª capa (dormidos)."),
        _f("2️⃣", f"Pero SÍ tenía vida: vendía {x.u_sem_epoca_viva:,.1f} u/sem cuando aún vendía",
           "no es un modelo que nunca funcionó — funcionaba y se apagó. Eso es lo que vale la pena diagnosticar."),
    ]
    if pd.notna(x.get("stock_venta")) and x.stock_venta > 0:
        meses_txt = (f" Si volviera a vender al ritmo de cuando vendía, hay para "
                     f"<b>{x.meses_stock_era:,.1f} meses</b>."
                     if pd.notna(x.get("meses_stock_era")) else "")
        factores.append(_f("3️⃣", f"Hay {x.stock_venta:,.0f} piezas paradas = "
                           f"{_usd(float(x.capital_atrapado),0)} de capital atrapado",
                           f"stock en almacenes de venta, sin apartada. Mientras no rote, "
                           f"ese dinero no trabaja.{meses_txt}"))
    else:
        factores.append(_f("3️⃣", "Sin stock disponible",
                           "no hay nada que vender: la acción es de compras/surtido, no de precio."))
    d_ = str(x.direccion)
    if d_ == "REACTIVAR":
        factores.append(_f("4️⃣", f"El factor decisivo: la lista subió{delta_txt} y la venta murió",
                           f"vendía a {_usd(float(x.precio_epoca_viva))} y hoy está en {_usd(p_hoy)}. "
                           f"El aumento coincide con la muerte de la venta ⇒ QUEDÓ CARO."))
        if x.get("incremento_laboratorio") == True:
            factores.append(_f("🧪", "Y el aumento fue 'de laboratorio': nunca compramos al costo nuevo",
                               f"el costo del stock en mano sigue siendo el viejo ({_usd(x.costo_stock_mano) if pd.notna(x.get('costo_stock_mano')) else '—'}); "
                               f"vender a su último precio con ventas ES rentable."))
    elif d_ == "LIQUIDAR":
        factores.append(_f("4️⃣", "El factor decisivo: se bajó el precio y la venta NO reaccionó",
                           "cuando ni el descuento revive la demanda, el problema no es precio: es obsolescencia. "
                           "Recuperar el capital vale más que esperar."))
    elif d_ == "EVALUAR CONTINUIDAD":
        factores.append(_f("4️⃣", "El factor decisivo: murió SIN cambio de lista",
                           "el precio no se movió — la demanda se fue sola (mercado, sustituto, fin de ciclo del producto). "
                           "El precio no la va a traer de vuelta: es decisión de surtido."))
    elif d_ == "SOLO PROYECTO":
        factores.append(_f("4️⃣", "El factor decisivo: solo vende por proyecto",
                           "sus ventas de la ventana fueron por proyecto (negociación puntual), no demanda recurrente — "
                           "el precio de lista no gobierna esa venta."))
    elif d_ == "ESPERAR STOCK":
        factores.append(_f("4️⃣", "El factor decisivo: no hay stock pero viene reposición",
                           "regla stock-first: el precio solo se actúa sobre lo que tiene stock; al llegar la reposición se re-evalúa (queda en seguimiento diario)."))
    if d_ == "REABASTECIDO: RE-EVALUAR":
        factores.append(_f("4️⃣", "El factor decisivo: la pausa fue de ABASTO, no de demanda",
                           _safe_txt(x.explicacion)))
    if d_ == "VENDE POR PRESENTACIÓN":
        factores.append(_f("4️⃣", "El factor decisivo: SÍ vende — por carrete /KM",
                           _safe_txt(x.explicacion)))
    accion = {"REACTIVAR": f"Regresar a su último precio con ventas: <b>{sug}</b>. Directo, sin paso gradual — un modelo muerto no tiene clientes que perturbar.",
              "LIQUIDAR": "Liquidar para recuperar capital: descuento agresivo o canal de remate.",
              "EVALUAR CONTINUIDAD": "Decisión de surtido con el proveedor/compras: ¿se mantiene en catálogo?",
              "SOLO PROYECTO": "Sin acción de lista: se cotiza por proyecto cuando aparezca.",
              "ESPERAR STOCK": "Esperar la reposición; el seguimiento diario avisa cuando llegue.",
              "VENDE POR PRESENTACIÓN": "Ninguna acción en este código: la decisión de precio va en el código del carrete (/KM), que es el que vende.",
              "REABASTECIDO: RE-EVALUAR": "Tratarlo como reinicio: si la lista subió durante la pausa, arrancar del último precio probado con ventas y medir; si no se movió, darle semanas de prueba con stock."}.get(d_, "")
    D = {"id": sid, "cid": f"chart_{sid}", "sid_": f"pstrip_{sid}",
         "sem": semanas, "u": unidades, "p": listas, "cambios": cambios,
         "yMax": int(np.ceil(max(max(unidades), 1) * 1.15 / 10) * 10) or 10,
         "nProy": 3, "proy": 0, "mant": 0, "bLo": 0.0, "bHi": 0.0,
         "pSug": float(x.precio_sugerido) if pd.notna(x.precio_sugerido) else p_hoy,
         "dir": str(x.direccion)}
    html = f"""
  <div class="top">
    {volver_html}
    <span style="font-size:20px;font-weight:700;color:var(--azul)">{codigo}</span>
    <span class="badge b-gris">💤 DORMIDO (2ª capa)</span>
    <span class="badge {dir_cls}">{x.direccion}</span>
    {hist_badge}
    <span style="font-size:12px;color:var(--gris)">{_safe_txt(x.get('proveedor'))}</span>
  </div>
  <div class="grid2">
    <div class="card" style="border-left:4px solid var(--azul)">
      <div class="sec-t">¿Por qué está clasificado como dormido — {_safe_txt(x.diagnostico).split(' (')[0]}?</div>
      <div class="sec-s">Los factores que el motor evaluó, en orden:</div>
      <div class="ev"><ul style="margin:6px 0;padding-left:18px;list-style:none">
        {''.join(factores)}
      </ul></div>
    </div>
    <div class="card" style="border-left:4px solid var(--verde)">
      <div class="sec-t">Qué hacer</div>
      <div class="ev" style="font-size:14px;line-height:1.7">{accion}</div>
      <div class="ev"><ul style="margin:6px 0;padding-left:18px">
        {dos_costos}
        <li style="color:var(--gris);font-size:12px">Estatus completo: {_safe_txt(x.explicacion)}</li>
      </ul></div>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="sec-t">La historia en una imagen — {len(s)} semanas con venta</div>
    <div class="sec-s">Se ve dónde vivía, dónde murió y qué pasó con el precio (banda inferior). La proyección
      en 0 es literal: si nada cambia, este modelo no vende.</div>
    <div style="padding:0 8px 4px"><svg id="chart_{sid}" width="100%" height="300" viewBox="0 0 1040 300" role="img" aria-label="Unidades por semana"></svg></div>
    <div style="padding:0 8px 10px"><svg id="pstrip_{sid}" width="100%" height="64" viewBox="0 0 1040 64" role="img" aria-label="Precio de lista por semana"></svg></div>
  </div>
  {_tabla_meses(s, ctx["pan"].semana.max())}
  <div class="card" style="margin-top:14px">
    <div class="sec-t">Película de costos (ventana de 24 meses)</div>
    <div class="sec-s">Cambios de costo de reposición ≥2% (últimos 8) — el contexto de la regla de los dos costos</div>
    <table class="num"><tr><th>Semana</th><th>Antes</th><th>Después</th></tr>{filas_costo}</table>
  </div>
"""
    return html, D


def _safe_txt(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


JS_LIB = """
function renderSku(D){
  const svg = document.getElementById(D.cid);
  if(!svg || svg.dataset.done) return; svg.dataset.done = 1;
  const W=1040,H=300,L=52,R=14,T=16,B=34, NS="http://www.w3.org/2000/svg";
  const n=D.u.length+D.nProy, x=i=>L+i*(W-L-R)/(n-1), y=v=>T+(1-v/D.yMax)*(H-T-B);
  const el=(t,a,parent)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);(parent||svg).appendChild(e);return e;};
  const paso=D.yMax/4;
  for(let g=0;g<=4;g++){const v=g*paso;
    el("line",{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:"#eef0f3","stroke-width":1});
    const tx=el("text",{x:L-8,y:y(v)+4,"text-anchor":"end","font-size":10,fill:"#9ca3af"});
    tx.textContent=v>=1000?(Math.round(v/100)/10)+"k":String(Math.round(v));}
  const cadaX = Math.max(1, Math.round(D.sem.length/7));
  D.sem.forEach((s,i)=>{if(i%cadaX===0){const tx=el("text",{x:x(i),y:H-12,"text-anchor":"middle","font-size":10,fill:"#9ca3af"});tx.textContent=s;}});
  const x0p=x(D.u.length-1), x1p=x(n-1);
  el("path",{d:`M${x0p},${y(D.bLo)} L${x1p},${y(D.bLo)} L${x1p},${y(D.bHi)} L${x0p},${y(D.bHi)} Z`,fill:"#2563eb",opacity:.07});
  const bt=el("text",{x:x1p-4,y:y(D.bLo)-6,"text-anchor":"end","font-size":10,fill:"#6b7280"});
  bt.textContent=`rango p10–p90: ${D.bLo.toLocaleString()}–${D.bHi.toLocaleString()}`;
  D.cambios.forEach((c,k)=>{
    el("line",{x1:x(c.i),x2:x(c.i),y1:T,y2:H-B,stroke:"#9ca3af","stroke-width":1,"stroke-dasharray":"3 3"});
    if(c.lbl){const anc=x(c.i)>W*.6?"end":"start";
      const tx=el("text",{x:anc==="start"?x(c.i)+4:x(c.i)-4,y:T+10+(k%2)*12,"text-anchor":anc,"font-size":10,fill:"#6b7280"});
      tx.textContent=c.lbl;}});
  el("path",{d:`M${x(0)},${y(0)} L`+D.u.map((v,i)=>`${x(i)},${y(v)}`).join(" L")+` L${x(D.u.length-1)},${y(0)} Z`,fill:"#2563eb",opacity:.08});
  el("polyline",{points:D.u.map((v,i)=>`${x(i)},${y(v)}`).join(" "),fill:"none",stroke:"#2563eb","stroke-width":2,"stroke-linejoin":"round","stroke-linecap":"round"});
  const lastX=x(D.u.length-1), lastY=y(D.u[D.u.length-1]);
  el("polyline",{points:`${lastX},${lastY} ${x1p},${y(D.mant)}`,fill:"none",stroke:"#9ca3af","stroke-width":2,"stroke-dasharray":"5 4"});
  const t2=el("text",{x:x1p-6,y:y(D.mant)-24,"text-anchor":"end","font-size":11,fill:"#6b7280"});
  t2.textContent=`mantener → ${D.mant.toLocaleString()}`;
  if(D.dir!=="MANTENER"){
    el("polyline",{points:`${lastX},${lastY} ${x1p},${y(D.proy)}`,fill:"none",stroke:"#2563eb","stroke-width":2,"stroke-dasharray":"5 4"});
    el("circle",{cx:x1p,cy:y(D.proy),r:3.5,fill:"#2563eb"});
    const t1=el("text",{x:x1p-6,y:y(D.proy)-8,"text-anchor":"end","font-size":11,"font-weight":700,fill:"#2563eb"});
    t1.textContent=`aplicar $${D.pSug.toLocaleString()} → ${D.proy.toLocaleString()}`;}
  const tip=document.getElementById("tip");
  const cross=el("line",{x1:0,x2:0,y1:T,y2:H-B,stroke:"#1f2937","stroke-width":1,opacity:0});
  const dot=el("circle",{r:4,fill:"#2563eb",stroke:"#fff","stroke-width":2,opacity:0});
  svg.addEventListener("mousemove",ev=>{
    const r=svg.getBoundingClientRect(), mx=(ev.clientX-r.left)*W/r.width;
    const i=Math.max(0,Math.min(D.u.length-1,Math.round((mx-L)/((W-L-R)/(n-1)))));
    cross.setAttribute("x1",x(i));cross.setAttribute("x2",x(i));cross.setAttribute("opacity",.25);
    dot.setAttribute("cx",x(i));dot.setAttribute("cy",y(D.u[i]));dot.setAttribute("opacity",1);
    tip.innerHTML=`<b>${D.sem[i]}</b> · ${D.u[i].toLocaleString()} unidades · lista $${D.p[i].toFixed(2)}`;
    tip.style.opacity=1;tip.style.left=(ev.clientX+14)+"px";tip.style.top=(ev.clientY-10)+"px";});
  svg.addEventListener("mouseleave",()=>{tip.style.opacity=0;cross.setAttribute("opacity",0);dot.setAttribute("opacity",0);});
  // franja de precio
  const sp=document.getElementById(D.sid_);
  if(!sp) return;
  const el2=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);sp.appendChild(e);return e;};
  const pAll=D.p.concat([D.pSug]), pMin=Math.min(...pAll)*.97, pMax=Math.max(...pAll)*1.03;
  const yp=v=>8+(1-(v-pMin)/(pMax-pMin))*36;
  let d=`M${x(0)},${yp(D.p[0])}`;
  for(let i=1;i<D.p.length;i++) d+=` L${x(i)},${yp(D.p[i-1])} L${x(i)},${yp(D.p[i])}`;
  el2("path",{d,fill:"none",stroke:"#6b7280","stroke-width":2});
  if(D.dir!=="MANTENER"){
    el2("line",{x1:x(D.u.length-1),x2:x(n-1),y1:yp(D.p[D.p.length-1]),y2:yp(D.pSug),stroke:"#2563eb","stroke-width":2,"stroke-dasharray":"5 4"});
    const st=el2("text",{x:x(n-1)-4,y:yp(D.pSug)-5,"text-anchor":"end","font-size":10,"font-weight":700,fill:"#2563eb"});
    st.textContent=`$${D.pSug.toFixed(2)} sugerido`;}
  const lt=el2("text",{x:L-8,y:yp(D.p[0])+4,"text-anchor":"end","font-size":10,fill:"#9ca3af"});
  lt.textContent="lista";
  const p0=el2("text",{x:x(0)+4,y:yp(D.p[0])-5,"font-size":10,fill:"#6b7280"});
  p0.textContent=`$${D.p[0].toFixed(2)}`;
  D.cambios.forEach(c=>{if(c.lbl){const anc=x(c.i)>W*.6?"end":"start";
    const tx=el2("text",{x:anc==="start"?x(c.i)+4:x(c.i)-4,y:yp(c.despues)-5,"text-anchor":anc,"font-size":10,fill:"#6b7280"});
    tx.textContent=`$${c.despues.toFixed(2)}`;}});
}
"""

PIE = ("Guardrails activos: paso máx ±4% por ciclo de 3 semanas · piso de margen neto +3pts · sobrestock ≥12m no sube · "
       "abstención a baja confianza · pronóstico de <b>ventas observables</b> · "
       "Motor de Precio Óptimo v3 · fuente reporte_61 + valor_inventario")


def generar(codigo, ctx=None):
    ctx = ctx or cargar_ctx()
    cuerpo, D = cuerpo_sku(codigo, ctx, volver_html='<a href="#">← Subir precios</a>')
    html = (f'<meta charset="utf-8">\n<title>{codigo} — detalle · Motor de Precio Óptimo v3</title>\n'
            + ESTILOS
            + f'\n<div class="wrap">{cuerpo}<div class="foot">{PIE} · panel_sku.py</div></div>'
            + '\n<div class="tip" id="tip"></div>'
            + f'\n<script>{JS_LIB}\nrenderSku({json.dumps(D, ensure_ascii=False)});</script>')
    ruta = os.path.join(BASE, "out", f"panel_sku_{_safe(codigo)}.html")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"panel: {ruta}", flush=True)
    return ruta


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("uso: panel_sku.py <CODIGO>")
    generar(sys.argv[1])
