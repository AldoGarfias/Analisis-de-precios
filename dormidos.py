# -*- coding: utf-8 -*-
"""SEGUNDA CAPA: los modelos dormidos — activos históricamente pero SIN venta
reciente (los que el motor principal no evalúa por falta de venta).

Por qué: un producto que dejó de venderse puede ser víctima de un precio mal
calculado — y el motor principal no lo ve porque exige venta reciente. Regla
del negocio: evaluar sus ventas de los últimos 12 meses; si no hay nada,
ampliar a 24; y revisar sus MOVIMIENTOS de precio y costo — quizá quedó caro
o algo pasó.

Cómo (todo con datos locales):
  - Venta: panel de 24 meses (unidades recurrentes).
  - Precio VIGENTE aunque no venda: la lista administrada (p1/p3) viene en el
    snapshot semanal de valor_inventario para TODO el catálogo.
  - Punto de comparación: la lista/margen/costo de su última época CON VENTAS
    (mediana de sus últimas 8 semanas con venta).

RELOJ DE MUERTE EFECTIVA (usuario 2026-07-31; afinado 2026-08-04): la edad
de un dormido son sus semanas sin venta con stock SUFICIENTE — dos capas:
(1) disp_venta > 0 y (2) el disponible cubre ≥1 MES de su venta de época viva
(evita clasificar como dormido lo que fue tema de DISPONIBILIDAD, no de
precio; caso testigo DSMCW417/64G/GLE). El calendario queda informativo.
Afinación futura aprobada: cruce por SUCURSAL (¿tenía stock donde más vende?)
— requiere stock por almacén, hoy no extraído.

Direcciones que emite:
  REACTIVAR              quedó caro ⇒ volver a su lista de época viva; con ≥6/12
                         meses muerto, escalón adicional ×0.90/×0.80 (piso costo+3)
  LIQUIDAR               bajó y no reaccionó ⇒ obsolescencia probable
  EVALUAR CONTINUIDAD    murió sin cambio de lista ⇒ la causa no fue el precio
  ESPERAR STOCK / PAUSA POR FALTA DE STOCK   sin inventario que accionar
  REABASTECIDO: RE-EVALUAR   llegó stock nuevo ⇒ recibirlo a precio vigente
                         (evidencia reabastos: mantener ≫ subir), piso costo+3
  SOLO PROYECTO          vende por proyecto, sin señal recurrente de precio
  VENDE POR PRESENTACIÓN /KM (FiberHome: el carrete vende por el individual)

CADENCIA (usuario 2026-07-31, "desde la semana 8"): LIQUIDAR/EVALUAR reciben
precio sugerido por escalera de recorte acumulado vs su lista pre-silencio —
8-15 sem: −5% (o el recorte de su grupo proveedor si su evidencia respalda),
16-25: −10%, 26-51: −15%, ≥52: −25%; siempre ≥ piso costo-del-stock+3pts.
Antes de la semana 8 de muerte efectiva NO se sugiere recorte. Re-decisión
cada 4 semanas CON stock: recorte ≥4 sem sin revivir y sin respaldo del grupo
⇒ REVERTIR. Evidencia: analisis_reactivacion.py (parquets A reabastos y B
silencios; por proveedor hay dos mundos — la regla exige Δ≥+10pts, n≥20/brazo).

REMATE/CLASIFICACIÓN (ERP, censo de inventario): MI/MC = muestras invendibles
⇒ excluidos; remate S o clasificación R0-R4 dormido ⇒ rematarlo MÁS (+10% de
profundidad, tope 35%, nunca revertir, sugerir subir de nivel R).

Prioridad: capital atrapado = stock disponible × costo de cuando aún vendía.
Salida: out/segunda_capa_dormidos.csv + resumen impreso. El reporte
(reporte_top.py) muestra la sección "Dormidos (2ª capa)" con estos datos.
"""
import glob
import os
import re

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

# REGLA /KM (usuario 2026-07-27, acotada por el usuario a FIBERHOME): los
# modelos del proveedor FiberHome que terminan en "/NKM" son la presentación
# vendible (carrete de N km) del modelo INDIVIDUAL (mismo código sin sufijo).
# Las variantes están marcadas kit='Si' (excluidas del panel), así que el
# individual parece muerto aunque venda. Venta individual = cantidad × N.
PAT_KM = re.compile(r"^(.+)/(\d+(?:\.\d+)?)KM$", re.I)


def _venta_por_presentacion(bases, provs):
    """Ventas EQUIVALENTES (unidades individuales) de las variantes /NKM de
    FIBERHOME para los códigos en `bases`, leyendo las ventas crudas (las
    variantes no están en el panel: son kits). Devuelve dict base → info."""
    kits = pd.read_parquet(os.path.join(DATA, "reporte61", "kits.parquet"))
    cand = {}
    for c in kits.codigo.astype(str):
        m = PAT_KM.match(c)
        if not (m and m.group(1) in bases):
            continue
        base_ = m.group(1)
        pv = str(provs.get(base_, "")) + " " + str(provs.get(c, ""))
        if "fiberhome" not in pv.lower():
            continue                       # regla acotada a FiberHome
        cand[c] = (base_, float(m.group(2)))
    if not cand:
        return {}
    regs = []
    for ruta in sorted(glob.glob(os.path.join(DATA, "reporte61", "ventas_*.parquet"))):
        d = pd.read_parquet(ruta, columns=["codigo", "fecha", "cantidad", "precio"])
        sub = d[d.codigo.isin(cand)]
        if len(sub):
            regs.append(sub)
    if not regs:
        return {}
    v = pd.concat(regs, ignore_index=True)
    v["cantidad"] = v.cantidad.astype(float)
    v = v[(v.cantidad > 0) & (v.precio.astype(float) > 0)]
    v["base"] = v.codigo.map({k: b for k, (b, _) in cand.items()})
    v["factor"] = v.codigo.map({k: f for k, (_, f) in cand.items()})
    v["u_eq"] = v.cantidad * v.factor
    v["fecha"] = pd.to_datetime(v.fecha)
    corte = v.fecha.max()
    out = {}
    for b, g in v.groupby("base"):
        rec = g[g.fecha >= corte - pd.Timedelta(weeks=12)]
        out[b] = {
            "variantes": ", ".join(sorted(g.codigo.unique())),
            "u_eq_sem_12s": round(float(rec.u_eq.sum()) / 12, 1),
            "lineas_12s": int(len(rec)),
            "ult_venta_var": g.fecha.max().date().isoformat(),
        }
    return out

UMBRAL_PRECIO = 0.05   # ±5% para considerar que la lista se movió
# (la población dormida se define por exclusión del motor principal, no por umbral propio)


def _series_costos(ex):
    """Pivotes SEMANALES (24 meses) de costo de reposición y costo del stock
    en mano. Solo con el esquema nuevo del snapshot (costo_prov/valor_stock)."""
    if "costo_prov" not in ex.columns:
        return None
    sem = np.sort(ex.semana.unique())
    cp = (ex.pivot_table(index="codigo", columns="semana", values="costo_prov",
                         aggfunc="last").reindex(columns=sem).ffill(axis=1))
    vs = (ex.pivot_table(index="codigo", columns="semana", values="valor_stock",
                         aggfunc="last").reindex(columns=sem))
    et = (ex.pivot_table(index="codigo", columns="semana", values="existencia",
                         aggfunc="last").reindex(columns=sem))
    cs = (vs / et.where(et > 0)).ffill(axis=1)
    return {"sem": sem, "cp": cp, "cs": cs}


def _historia_costo(sc, cod):
    """La PELÍCULA del costo en 24 meses para un SKU:
      - cambios del costo de reposición (≥2%), cuántos y cuándo
      - vigencia del costo actual (semanas desde el último cambio)
      - ¿COMPRAMOS después del último aumento? (el costo del stock en mano
        convergió ≥50% hacia el costo nuevo = sí compramos; plano = laboratorio)
    """
    if sc is None or cod not in sc["cp"].index:
        return None
    cp = sc["cp"].loc[cod].values
    cs = sc["cs"].loc[cod].values if cod in sc["cs"].index else None
    cambios = []
    for i in range(1, len(cp)):
        if np.isfinite(cp[i - 1]) and cp[i - 1] > 0 and np.isfinite(cp[i]):
            d = cp[i] / cp[i - 1] - 1
            if abs(d) >= 0.02:
                cambios.append((i, d))
    if not cambios:
        return {"n_cambios": 0, "sem_vigente": len(cp), "delta_ult": 0.0,
                "compro": None, "fecha_ult": None}
    i_ult, d_ult = cambios[-1]
    compro = None
    if cs is not None and d_ult > 0 and np.isfinite(cs).any():
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                cs_antes = np.nanmedian(cs[max(0, i_ult - 4):i_ult])
                cs_desp = np.nanmedian(cs[i_ult:])
        if np.isfinite(cs_antes) and cs_antes > 0 and np.isfinite(cs_desp):
            convergencia = (cs_desp / cs_antes - 1) / max(d_ult, 1e-9)
            compro = bool(convergencia >= 0.5)
    return {"n_cambios": len(cambios), "sem_vigente": len(cp) - i_ult,
            "delta_ult": d_ult, "compro": compro,
            "fecha_ult": pd.Timestamp(sc["sem"][i_ult]).date().isoformat()}


def correr():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    pan = pan[pan.activo].copy()
    pan["u_total"] = pan["unidades"].astype(float)   # CON proyectos
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)  # recurrente
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    # sello de corte (auditoría 2026-07-31, N9 — la partición dormidos =
    # activos − motor exige que recos y panel sean del MISMO corte)
    if "corte" in recos.columns:
        c_pan = pd.Timestamp(pan.semana.max()).date().isoformat()
        if str(recos.corte.iloc[0]) != c_pan:
            raise SystemExit(f"SELLO DE CORTE: recos al {recos.corte.iloc[0]} pero panel "
                             f"al {c_pan} — re-corre escenarios.py antes de dormidos")
    ex = pd.read_parquet(os.path.join(DATA, "reporte61", "existencias_sem.parquet"))
    # guard anti-snapshot-parcial: si hay una re-extracción en curso, el archivo
    # puede cubrir menos semanas que el backup — usar el más COMPLETO
    ruta_bak = os.path.join(DATA, "reporte61", "existencias_sem_backup.parquet")
    if os.path.exists(ruta_bak):
        bak = pd.read_parquet(ruta_bak)
        if bak.semana.max() > ex.semana.max():
            print(f"  (snapshot en re-extracción: usando backup completo "
                  f"hasta {pd.Timestamp(bak.semana.max()).date()})", flush=True)
            ex = bak

    semanas = np.sort(pan.semana.unique())
    u = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                         aggfunc="sum").reindex(columns=semanas))
    ut = (pan.pivot_table(index="codigo", columns="semana", values="u_total",
                          aggfunc="sum").reindex(columns=semanas))
    # dormidos = activos que NO están en el motor principal
    dormidos = sorted(set(u.index) - set(recos.codigo))
    print(f"activos históricos: {len(u):,} | en motor principal: {recos.codigo.nunique():,} "
          f"| DORMIDOS (2ª capa): {len(dormidos):,}", flush=True)
    # regla /KM FiberHome: ventas del carrete → unidades del individual
    ruta_prov0 = os.path.join(DATA, "reporte61", "proveedores.parquet")
    provs0 = (pd.read_parquet(ruta_prov0).set_index("codigo").proveedor
              if os.path.exists(ruta_prov0) else pd.Series(dtype=object))
    pres = _venta_por_presentacion(set(dormidos), provs0)
    print(f"  regla /KM FiberHome: {len(pres)} dormidos venden por carrete", flush=True)
    # pivote de stock semanal: ¿el silencio coincidió con FALTA DE STOCK?
    col_d = "disp_venta" if "disp_venta" in ex.columns else "disponible"
    pv_disp = (ex.pivot_table(index="codigo", columns="semana", values=col_d,
                              aggfunc="last"))

    # lista vigente HOY (p3 o p1 según su tipo modal) + stock, del último snapshot
    ex_ult = ex[ex.semana == ex.semana.max()].set_index("codigo")
    tipo_modal = pan.groupby("codigo").tipo_precio.agg(lambda s: s.mode().iat[0])
    # listas administradas SEMANALES (para la cadencia de dormidos: detectar
    # recortes hechos durante el silencio y su antigüedad)
    pv_p1 = ex.pivot_table(index="codigo", columns="semana", values="p1", aggfunc="last")
    pv_p3 = ex.pivot_table(index="codigo", columns="semana", values="p3", aggfunc="last")
    col_stock = "disp_venta" if "disp_venta" in ex_ult.columns else "disponible"
    # proveedor (para el filtro del reporte): censo de VENTAS (45 días) con
    # RESPALDO del censo de INVENTARIO (2026-07-31: un dormido no vende, así
    # que el 77% quedaba sin proveedor — el inventario sí lo trae para todo
    # lo que tiene stock; extract_api.py proveedores)
    ruta_prov = os.path.join(DATA, "reporte61", "proveedores.parquet")
    provs = (pd.read_parquet(ruta_prov).set_index("codigo").proveedor
             if os.path.exists(ruta_prov) else pd.Series(dtype=object))
    ruta_pinv = os.path.join(DATA, "reporte61", "proveedores_inventario.parquet")
    clasif_map, remate_map = pd.Series(dtype=object), pd.Series(dtype=object)
    if os.path.exists(ruta_pinv):
        _pi = pd.read_parquet(ruta_pinv).set_index("codigo")
        pinv = _pi.proveedor
        provs = provs.combine_first(pinv) if len(provs) else pinv
        if "clasificacion" in _pi.columns:
            clasif_map = _pi.clasificacion.fillna("")
            remate_map = (_pi.remate.fillna("N").eq("S")
                          | clasif_map.isin(["R0", "R1", "R2", "R3", "R4"]))
    # EVIDENCIA DE RESURRECCIÓN por proveedor (usuario 2026-07-31: "en lugar de
    # descripciones, un precio sugerido — ¿qué comportamiento ves en TODOS los
    # datos?; si el SKU no alcanza, agrupa por proveedor"). Del estudio de
    # 10,149 silencios-con-stock (analisis_reactivacion.py): GLOBALMENTE bajar
    # NO revive (45% vs 49% manteniendo), pero por proveedor hay dos mundos
    # (United Radio +40pts, RF Industries +44 vs Panduit −15, ICOM −38).
    # Regla: recorte solo si SU grupo lo respalda (Δ ≥ +10pts, n≥20 por brazo);
    # si no, MANTENER explícito con la evidencia en la explicación.
    ev_rev, ev_global = {}, None
    ruta_ev = os.path.join(DATA, "analisis_reactivacion_B.parquet")
    if os.path.exists(ruta_ev):
        evb = pd.read_parquet(ruta_ev)
        ev_global = (float(evb[evb.accion == "BAJÓ"].revivio.mean()),
                     float(evb[evb.accion == "IGUAL"].revivio.mean()))
        for pr_, d_ in evb.groupby("proveedor"):
            b_, g_ = d_[d_.accion == "BAJÓ"], d_[d_.accion == "IGUAL"]
            if pr_ and len(b_) >= 20 and len(g_) >= 20:
                exitosos = -b_[b_.revivio].magnitud
                rec = float(exitosos.median()) if len(exitosos) else 0.15
                ev_rev[pr_] = {"rb": float(b_.revivio.mean()),
                               "ri": float(g_.revivio.mean()),
                               "n": int(len(d_)),
                               "recorte": min(max(rec, 0.05), 0.25)}
    # evidencia global como TEXTO (ronda 3: cifras vivas del parquet, no congeladas)
    txt_glob = (f"{100*ev_global[0]:.0f}% vs {100*ev_global[1]:.0f}%"
                if ev_global else "45% vs 49%")
    # medianas del estudio A (reabastos) para el texto de REABASTECIDO
    reab_n, reab_ig, reab_su = 23415, 0.82, 0.72
    ruta_evA = os.path.join(DATA, "analisis_reactivacion_A.parquet")
    if os.path.exists(ruta_evA):
        eva = pd.read_parquet(ruta_evA)
        reab_n = len(eva)
        reab_ig = float(eva[eva.rel_precio.abs() < 0.02].recuperacion.median())
        reab_su = float(eva[eva.rel_precio >= 0.02].recuperacion.median())
    # la película de costos de 24 meses (requiere snapshot con esquema nuevo)
    sc = _series_costos(ex)
    if sc is None:
        print("  (snapshot sin columnas de costo: análisis histórico de costos "
              "pendiente de la re-extracción)", flush=True)

    filas, n_mimc = [], 0
    for cod in dormidos:
        su = u.loc[cod]
        con_venta = su.dropna()
        con_venta = con_venta[con_venta > 0]
        if con_venta.empty:
            continue
        ult_sem = con_venta.index.max()
        sem_sin_venta = int((semanas[-1] - np.datetime64(ult_sem)) / np.timedelta64(7, "D"))
        # RELOJ DE MUERTE EFECTIVA (usuario 2026-07-31, afinado 2026-08-04):
        # solo cuentan las semanas sin venta con stock SUFICIENTE — dos capas:
        #   1) disp_venta > 0, y
        #   2) el disponible cubre ≥1 MES de su venta de época viva (caso
        #      DSMCW417/64G/GLE: 27 pzas para un modelo de 28/sem es stock
        #      testimonial — la venta pudo frenarse por DISPONIBILIDAD, no por
        #      precio; el objetivo es no clasificar como dormido lo que fue
        #      tema de entrega). El calendario (sem_sin_venta) queda informativo.
        u_sem_era = float(con_venta.iloc[-8:].mean())
        umbral_d = max(1.0, u_sem_era * 4.345)   # 1 mes de su venta viva
        sem_muertas = sem_sin_venta
        if cod in pv_disp.index:
            # recortar al corte del PANEL: existencias_sem corre ~1 semana
            # adelante y esa semana aún no sabe si hubo venta (ronda 3)
            # DESDE CUÁNDO hay stock (usuario 2026-08-04): la semana cuenta
            # solo si el stock suficiente YA ESTABA desde la semana anterior —
            # la semana que recibe la reposición no cuenta (si llegó el 30 de
            # junio, junio no se toma; el snapshot es del lunes y el stock
            # necesita estar el periodo completo + distribuirse a sucursales)
            serie_d = pv_disp.loc[cod].fillna(0)
            ok_d = (serie_d >= umbral_d) & (serie_d.shift(1) >= umbral_d)
            sil_d = ok_d[(ok_d.index > ult_sem) & (ok_d.index <= semanas[-1])]
            if len(sil_d):
                sem_muertas = int(sil_d.sum())
        meses_muertos = sem_muertas / 4.345
        u12 = float(su.iloc[-52:].fillna(0).sum())
        u24 = float(su.fillna(0).sum())
        capa = "12m" if u12 > 0 else "24m"   # regla: primero 12 meses; si nada, 24

        # última época con ventas: sus últimas 8 semanas con venta
        s_era = pan[(pan.codigo == cod) & (pan.semana.isin(con_venta.index[-8:]))]
        # tipo de precio de la ÉPOCA VIVA, no de toda la historia (ronda 3):
        # un SKU que migró lista↔oferta debe operar sobre su lista vigente
        tipo_era = int(tipo_modal.get(cod, 3))
        if len(s_era) and s_era.tipo_precio.notna().any():
            tipo_era = int(s_era.tipo_precio.mode().iat[0])
        precio_era = float(s_era.precio_lista.median())
        costo_era = float(s_era.costo_prom.median())
        neto_era = float(s_era.neto_prom.median())
        margen_era = (neto_era - costo_era) / neto_era if neto_era > 0 else np.nan

        # lista vigente hoy (aunque no venda) + stock + LOS DOS COSTOS:
        #   costo_prov (reposición: comprar HOY) vs costo del stock EN MANO
        precio_hoy, stock, repo = np.nan, np.nan, np.nan
        costo_prov_hoy, costo_stock = np.nan, np.nan
        if cod in ex_ult.index:
            fila_ex = ex_ult.loc[cod]
            col_p = "p1" if tipo_era == 1 else "p3"
            precio_hoy = float(fila_ex.get(col_p, np.nan))
            stock = float(fila_ex.get(col_stock, np.nan))
            repo = float(fila_ex.get("backorder", np.nan))
            costo_prov_hoy = float(fila_ex.get("costo_prov", np.nan))
            vs, et = float(fila_ex.get("valor_stock", np.nan)), float(fila_ex.get("existencia", np.nan))
            if np.isfinite(vs) and np.isfinite(et) and et > 0:
                costo_stock = vs / et  # costo PROMEDIO de lo que tenemos en mano
        d_precio = (precio_hoy / precio_era - 1) if (np.isfinite(precio_hoy)
                                                     and precio_era > 0) else np.nan
        # el costo RELEVANTE para vender el stock actual es el del stock en mano
        # (lo que pagamos), no el de reposición; fallback: costo de cuando aún vendía
        costo_rel = costo_stock if np.isfinite(costo_stock) and costo_stock > 0 else costo_era
        rho_era = neto_era / precio_era if precio_era > 0 else np.nan
        # lista mínima que respeta el piso de margen NETO (costo+3pts) HOY
        lista_min = (costo_rel / (rho_era * 0.97)
                     if np.isfinite(rho_era) and rho_era > 0 and np.isfinite(costo_rel)
                     else np.nan)
        # "incremento de LABORATORIO": el proveedor subió (≥5%) pero NO compramos
        # (el stock en mano sigue ≈ al costo de cuando aún vendía)
        laboratorio = (np.isfinite(costo_prov_hoy) and np.isfinite(costo_era) and costo_era > 0
                       and costo_prov_hoy >= costo_era * 1.05
                       and np.isfinite(costo_stock) and costo_stock <= costo_era * 1.03)

        # (u_sem_era ya calculado arriba para el umbral del reloj)
        cap = (stock * costo_era if np.isfinite(stock) and stock > 0
               and np.isfinite(costo_era) else 0)

        # ¿vende por PROYECTO recientemente? (últimas 12 semanas, unidades de
        # proyecto = totales − recurrentes). F3: dos casos según el stock.
        proy12 = 0.0
        if cod in ut.index:
            proy12 = float((ut.loc[cod].iloc[-12:].fillna(0)
                            - su.iloc[-12:].fillna(0)).sum())

        # diagnóstico + dirección + precio sugerido + explicación de estatus
        if cod in pres and pres[cod]["u_eq_sem_12s"] > 0:
            # /KM FiberHome: el individual NO está dormido — vende por carrete
            p_ = pres[cod]
            dx = f"VENDE POR PRESENTACIÓN /KM ({p_['u_eq_sem_12s']:,.1f} u equiv/sem)"
            direc, p_sug = "VENDE POR PRESENTACIÓN", np.nan
            expl = (f"NO está dormido: este modelo se vende por CARRETE ({p_['variantes']}), "
                    f"que está excluido del panel por ser kit. Venta individual equivalente "
                    f"(cantidad × km): {p_['u_eq_sem_12s']:,.1f} u/sem en las últimas 12 semanas "
                    f"({p_['lineas_12s']} líneas; última venta del carrete {p_['ult_venta_var']}). "
                    f"El stock del código individual SÍ rota a través del carrete — capital NO "
                    f"atrapado. Cualquier decisión de precio va en el código del carrete.")
        elif (cod in pv_disp.index
              and (lambda sil: len(sil) >= 4 and (sil.fillna(0) <= 0).mean() >= 0.5)
                  (pv_disp.loc[cod][pv_disp.columns > ult_sem])
              and np.isfinite(stock) and stock > 0):
            # REGLA 35126B (usuario 2026-07-27): el silencio coincidió con FALTA
            # DE STOCK — no es un dormido de precio: no había qué vender. Con
            # stock de vuelta, se re-evalúa como reinicio.
            sil = pv_disp.loc[cod][pv_disp.columns > ult_sem].fillna(0)
            pct_ss = 100 * (sil <= 0).mean()
            # racha FINAL con stock (el reabasto vigente), no la primera del silencio
            vals = (sil > 0).values
            sem_reab = 0
            for v_ in vals[::-1]:
                if not v_:
                    break
                sem_reab += 1
            dx = f"PAUSA POR FALTA DE STOCK ({pct_ss:.0f}% del silencio sin stock)"
            direc = "REABASTECIDO: RE-EVALUAR"
            subio = np.isfinite(precio_hoy) and precio_era > 0 and precio_hoy / precio_era - 1 >= UMBRAL_PRECIO
            p_sug = precio_era if subio else np.nan
            # PISO DE MARGEN (auditoría 2026-07-30, C5): si durante la pausa el
            # costo subió, el precio de época viva puede quedar bajo costo+3pts
            # — el piso es innegociable también aquí (misma regla que REACTIVAR)
            if subio and np.isfinite(lista_min) and lista_min > p_sug:
                p_sug = lista_min
            expl = (f"Sin venta desde {pd.Timestamp(ult_sem).date()}, pero el {pct_ss:.0f}% de esas "
                    f"semanas NO había stock — la pausa fue de ABASTO, no de demanda ni de precio. "
                    f"Reabastecido hace ~{sem_reab} sem ({stock:,.0f} uds hoy)."
                    + (f" OJO: la lista subió {100*(precio_hoy/precio_era-1):+.0f}% durante la pausa "
                       f"(${precio_era:,.2f} → ${precio_hoy:,.2f}) y ese precio casi no se ha probado "
                       f"en el mercado — punto de partida probado: su lista de época con ventas."
                       if subio else " La lista no se movió: darle semanas de prueba con stock.") )
        elif proy12 > 0 and not (np.isfinite(stock) and stock > 0):
            # SOLO PROYECTO y sin stock: el escenario SANO — se trae por
            # proyecto y su precio se negocia por trato. Sin acción de precio.
            dx, direc, p_sug = "VENDE SOLO POR PROYECTO (sin stock)", "SOLO PROYECTO", np.nan
            expl = (f"Sin venta recurrente desde {pd.Timestamp(ult_sem).date()}, pero vendió "
                    f"{proy12:,.0f} uds por PROYECTO en las últimas 12 semanas y NO hay stock "
                    f"— consistente con producto que se trae por proyecto. Su precio se "
                    f"negocia por trato (terreno del win-rate/cotizaciones), no por lista.")
        elif not (np.isfinite(stock) and stock > 0):
            # INVARIANTE stock-first: el precio SOLO se actúa sobre lo que tiene
            # stock. Sin stock: si hay reposición en camino → ESPERAR y
            # re-evaluar al llegar (patrón del seguimiento de frenos); si no
            # hay reposición → tema de compras.
            if np.isfinite(repo) and repo > 0:
                dx, direc, p_sug = "SIN STOCK, REPOSICIÓN EN CAMINO", "ESPERAR STOCK", np.nan
                expl = (f"Última venta {pd.Timestamp(ult_sem).date()}. Sin stock hoy pero con "
                        f"{repo:,.0f} uds en reposición. Regla del negocio: el precio no se "
                        f"actúa sin stock — queda en SEGUIMIENTO; al llegar la reposición se "
                        f"re-evalúa con el escenario de ese momento (lista de época con ventas "
                        f"${precio_era:,.2f} vs hoy ${precio_hoy:,.2f}).")
            else:
                dx, direc, p_sug = "SIN STOCK (compras, no precio)", "COMPRAS", np.nan
                expl = (f"Última venta {pd.Timestamp(ult_sem).date()}. Sin stock disponible en "
                        f"almacenes de venta ni reposición en camino: no hay qué vender — "
                        f"el tema es de compras, no de precio.")
        elif np.isfinite(d_precio) and d_precio >= UMBRAL_PRECIO:
            dx = f"QUEDÓ CARO (lista {100*d_precio:+.0f}% vs cuando aún vendía)"
            direc = "REACTIVAR"
            # corrección DIRECTA al su último precio con ventas: un producto muerto
            # no tiene clientes que perturbar (el paso gradual es para vivos)…
            # PERO respetando el piso de margen contra el costo del stock EN
            # MANO (F2): si el costo relevante ya no alcanza, el sugerido sube
            # al mínimo con piso.
            p_sug = precio_era
            nota_piso = ""
            if np.isfinite(lista_min) and lista_min > precio_era:
                p_sug = lista_min
                nota_piso = (f" OJO: su precio de época con ventas ya NO cubre el piso "
                             f"(costo del stock ${costo_rel:,.2f}); sugerido = mínimo "
                             f"con costo+3pts.")
            nota_lab = ""
            if laboratorio:
                nota_lab = (f" INCREMENTO DE LABORATORIO: el costo proveedor subió a "
                            f"${costo_prov_hoy:,.2f} ({100*(costo_prov_hoy/costo_era-1):+.0f}%) "
                            f"pero NO compramos — el stock en mano costó ${costo_stock:,.2f} "
                            f"(≈ costo de cuando aún vendía). Vender el stock actual al precio "
                            f"viejo ES rentable; re-evaluar el precio al recomprar al costo nuevo.")
            # la película de 24 meses: cuánto lleva vigente el costo nuevo y si
            # compramos después del cambio
            h = _historia_costo(sc, cod)
            nota_hist = ""
            if h and h["n_cambios"] > 0:
                compro_txt = ("y SÍ compramos después (stock convergió al costo nuevo)"
                              if h["compro"] else
                              ("y NO hemos comprado después" if h["compro"] is False else ""))
                nota_hist = (f" Historia 24m: {h['n_cambios']} cambio(s) de costo proveedor; "
                             f"el actual ({100*h['delta_ult']:+.0f}%) lleva {h['sem_vigente']} "
                             f"semanas vigente desde {h['fecha_ult']} {compro_txt}.")
            nota_lab += nota_hist
            expl = (f"Vendía ~{u_sem_era:,.0f} u/sem a ${precio_era:,.2f} hasta "
                    f"{pd.Timestamp(ult_sem).date()}; después la lista subió a "
                    f"${precio_hoy:,.2f} ({100*d_precio:+.0f}%) y la venta murió "
                    f"({sem_sin_venta} semanas sin vender). REACTIVAR: ${p_sug:,.2f} "
                    f"(margen de entonces: {100*margen_era:.0f}%).{nota_piso}{nota_lab} "
                    f"Stock parado: {stock:,.0f} uds = ${cap:,.0f}.")
        elif np.isfinite(d_precio) and d_precio <= -UMBRAL_PRECIO:
            dx, direc, p_sug = "BAJÓ Y NO REACCIONÓ (obsolescencia probable)", "LIQUIDAR", np.nan
            expl = (f"Dejó de vender el {pd.Timestamp(ult_sem).date()} (vendía "
                    f"~{u_sem_era:,.0f} u/sem); la lista ya BAJÓ {100*d_precio:.0f}% y aun "
                    f"así no reaccionó — el precio no es el problema: obsolescencia probable. "
                    f"Candidato a liquidación/salida. Stock parado: {stock:,.0f} uds = ${cap:,.0f}.")
        else:
            dx, direc, p_sug = "MURIÓ SIN CAMBIO DE LISTA (evaluar continuidad/mercado)", "EVALUAR CONTINUIDAD", np.nan
            expl = (f"Dejó de vender el {pd.Timestamp(ult_sem).date()} (vendía "
                    f"~{u_sem_era:,.0f} u/sem) SIN que la lista cambiara — la causa no fue el "
                    f"precio (mercado, reemplazo, canal). Evaluar continuidad. "
                    f"Stock parado: {stock:,.0f} uds = ${cap:,.0f}.")

        # ═══ CADENCIA DE DORMIDO (usuario 2026-07-31: "desde la semana 8 con
        # cero ventas empezar a sugerir bajar; cada 4 semanas de antigüedad,
        # sugerir más movimientos — revertir o continuar") ═══
        # Escalera OBJETIVO de recorte acumulado vs la lista pre-silencio,
        # por edad de MUERTE EFECTIVA (solo semanas con stock):
        #   8-15 sem: −5% (o el recorte del grupo si su evidencia es positiva)
        #   16-25:    −10%      26-51 (≈6-12m): −15% (recuperación de capital)
        #   ≥52 (12m+): −25%    · siempre ≥ piso costo-del-stock+3pts
        # Re-decisión cada 4 semanas: si el último recorte lleva ≥4 sem SIN
        # revivir y el grupo NO respalda profundizar ⇒ REVERTIR (evidencia
        # global 45% vs 49%: sostener un recorte que no revivió solo regala
        # margen); si el grupo respalda o toca peldaño de edad ⇒ profundizar.
        if direc == "REACTIVAR" and np.isfinite(p_sug) and p_sug > 0 \
                and meses_muertos >= 6:
            # REACTIVAR envejecido: su precio de época viva ya no basta —
            # escalón adicional hacia recuperación de capital (piso respetado)
            factor = 0.90 if meses_muertos < 12 else 0.80
            p_esc = max(p_sug * factor, lista_min if np.isfinite(lista_min) else 0.0)
            if p_esc < p_sug:
                p_sug = p_esc
                expl += (f" ESCALERA POR EDAD: {meses_muertos:.0f} meses muerto CON stock "
                         f"⇒ {100*(1-factor):.0f}% adicional bajo su época viva "
                         f"(recuperación de capital; piso costo+3pts respetado).")
        if direc == "REABASTECIDO: RE-EVALUAR" and not np.isfinite(p_sug) \
                and np.isfinite(precio_hoy) and precio_hoy > 0:
            # piso costo+3pts también aquí (ronda 3): si el costo del stock
            # nuevo dejó la lista vigente bajo el piso, recibir AL piso
            p_sug = precio_hoy
            nota_piso_r = ""
            if np.isfinite(lista_min) and lista_min > precio_hoy:
                p_sug = lista_min
                nota_piso_r = (f" (ajustado al piso costo+3pts: la lista vigente "
                               f"${precio_hoy:,.2f} quedó bajo el costo del stock)")
            expl += (f" PRECIO SUGERIDO: recibir el stock con su precio vigente"
                     f"{nota_piso_r} (evidencia de {reab_n:,} reabastos: mantener "
                     f"recupera {100*reab_ig:.0f}% de la venta previa; subirle al "
                     f"stock recién llegado la baja a {100*reab_su:.0f}%).")
        # MI/MC = muestras, no se venden ⇒ fuera del análisis (usuario 2026-07-31)
        if str(clasif_map.get(cod, "")) in ("MI", "MC"):
            n_mimc += 1
            continue
        en_remate = bool(remate_map.get(cod, False))
        if direc in ("LIQUIDAR", "EVALUAR CONTINUIDAD") \
                and np.isfinite(precio_hoy) and precio_hoy > 0:
            pr_cod = str(provs.get(cod, "") or "")
            e_ = ev_rev.get(pr_cod)
            grupo_ok = bool(e_ and (e_["rb"] - e_["ri"]) >= 0.10)
            if en_remate:
                # REMATE dormido (usuario: "si es remate y está dormido, hay
                # que rematarlo MÁS"): nunca revertir, peldaño +10% más
                # profundo, y sugerir subir de nivel R en el canal
                grupo_ok = True
            # lista semanal del silencio: p0 pre-silencio, recortes hechos y edad
            pv_l = pv_p3 if tipo_era == 3 else pv_p1
            p0, sem_corte = precio_hoy, None
            if cod in pv_l.index:
                serie = pv_l.loc[cod].ffill()
                pre = serie[serie.index <= ult_sem].iloc[-4:]
                if len(pre) and np.isfinite(pre.median()) and pre.median() > 0:
                    p0 = float(pre.median())
                sil_l = serie[serie.index > ult_sem].dropna()
                if len(sil_l):
                    caidas = sil_l[sil_l / p0 - 1 <= -0.02]
                    if len(caidas):
                        f_corte = caidas.index.max()
                        sem_corte = int((semanas[-1] - np.datetime64(f_corte))
                                        / np.timedelta64(7, "D"))
                        # el timer del recorte también corre en semanas CON
                        # stock (ronda 3): un recorte no se evalúa si el SKU
                        # pasó esas semanas sin inventario que vender
                        if cod in pv_disp.index:
                            s_c = pv_disp.loc[cod].fillna(0)
                            ok_c = (s_c >= umbral_d) & (s_c.shift(1) >= umbral_d)
                            d_c = ok_c[(ok_c.index > f_corte)
                                       & (ok_c.index <= semanas[-1])]
                            if len(d_c):
                                sem_corte = int(d_c.sum())
            aplicado = max(0.0, 1 - precio_hoy / p0) if p0 > 0 else 0.0
            # objetivo por edad (el recorte del grupo puede adelantar el 1er peldaño)
            e_edad = sem_muertas
            if e_edad < 16:
                objetivo = max(0.05, e_["recorte"] if (e_ and grupo_ok) else 0.0)
            elif e_edad < 26:
                objetivo = 0.10
            elif e_edad < 52:
                objetivo = 0.15
            else:
                objetivo = 0.25
            if en_remate:
                objetivo = min(0.35, objetivo + 0.10)   # rematarlo MÁS
            etiqueta_cap = " (recuperación de capital)" if e_edad >= 26 else ""
            piso_ = lista_min if np.isfinite(lista_min) else 0.0
            if e_edad < 8:
                # PUERTA DE LA SEMANA 8 (usuario 2026-07-31: "desde la semana 8
                # con cero ventas empezar a sugerir bajar") — antes de eso, la
                # cadencia NO emite recorte: mantener y esperar el reloj
                p_sug = precio_hoy
                expl += (f" CADENCIA: {sem_muertas:.0f} semanas muerto CON stock — "
                         f"la escalera de recortes empieza en la semana 8; "
                         f"MANTENER por ahora.")
            elif aplicado >= objetivo - 0.005 and sem_corte is not None and sem_corte >= 4 \
                    and not grupo_ok and e_edad < 26:
                # recorte vigente ≥4 sem (con stock) sin revivir y sin respaldo ⇒ REVERTIR
                p_sug = max(p0, piso_)
                expl += (f" CADENCIA: el recorte de {100*aplicado:.0f}% lleva "
                         f"{sem_corte} semanas con stock sin revivirlo — REVERTIR a "
                         f"${p_sug:,.2f}"
                         + ("" if p_sug == p0 else " (su lista pre-silencio, acotada al piso costo+3pts)")
                         + f": la evidencia global ({txt_glob}) "
                         f"dice que sostener un recorte que no funcionó solo regala "
                         f"margen. Siguiente peldaño de edad: {16 if e_edad < 16 else 26} "
                         f"semanas muertas.")
            elif aplicado >= objetivo - 0.005:
                # piso también al sostener (ronda 3): si el costo del stock dejó
                # la lista vigente bajo costo+3pts, subir AL piso
                p_sug = max(precio_hoy, piso_)
                nota_p = ("" if p_sug == precio_hoy else
                          f" Lista vigente bajo el piso costo+3pts ⇒ ajustar a ${p_sug:,.2f}.")
                if sem_corte is not None and sem_corte < 4:
                    expl += (f" CADENCIA: recorte fresco ({sem_corte} sem con stock) — "
                             f"evaluar en la semana 4 (revivir/revertir/profundizar)."
                             + nota_p)
                else:
                    expl += (f" CADENCIA: al peldaño objetivo de su edad "
                             f"({100*objetivo:.0f}%{etiqueta_cap}); re-decisión al "
                             f"siguiente peldaño (cada 4 semanas de antigüedad)." + nota_p)
            else:
                p_sug = max(p0 * (1 - objetivo), piso_)
                cl_ = str(clasif_map.get(cod, "") or "")
                base_ev = ("REMATE dormido ⇒ rematarlo MÁS (+10% de profundidad; "
                           f"clasificación {cl_ if cl_ and cl_ != 'nan' else 'S'}: considerar subir "
                           "de nivel R en el canal)" if en_remate else
                           (f"su grupo ({pr_cod[:30]}: bajar revivió {100*e_['rb']:.0f}% "
                            f"vs {100*e_['ri']:.0f}% en {e_['n']} silencios)"
                            if (e_ and grupo_ok) else
                            "escalera de edad" + etiqueta_cap))
                expl += (f" CADENCIA: {sem_muertas:.0f} semanas muerto CON stock ⇒ "
                         f"peldaño {100*objetivo:.0f}% vs su lista pre-silencio "
                         f"(${p0:,.2f}), por {base_ev}. Re-decisión en 4 semanas: "
                         f"si no revive, revertir o profundizar según su grupo."
                         + ("" if e_edad >= 26 or grupo_ok else
                            f" OJO: la evidencia global ({txt_glob}) dice que el recorte "
                            "rara vez revive por sí solo — acompañar con acción comercial."))

        # venta mezclada CON stock: el diagnóstico de precio aplica normal,
        # pero el stock no puede esperar al siguiente proyecto — advertirlo
        if proy12 > 0 and np.isfinite(stock) and stock > 0:
            expl += (f" ADEMÁS vendió {proy12:,.0f} uds por PROYECTO en las últimas 12 "
                     f"semanas — pero el stock parado NO puede esperar al siguiente "
                     f"proyecto: accionar el precio para rotarlo por canal recurrente.")

        filas.append({
            "codigo": cod, "capa": capa,
            "direccion": direc,
            "unidades_proyecto_12s": round(proy12, 0),
            "precio_sugerido": round(p_sug, 2) if np.isfinite(p_sug) else np.nan,
            "costo_prov_hoy": round(costo_prov_hoy, 2) if np.isfinite(costo_prov_hoy) else np.nan,
            "costo_stock_mano": round(costo_stock, 2) if np.isfinite(costo_stock) else np.nan,
            "costo_epoca_viva": round(costo_era, 2) if np.isfinite(costo_era) else np.nan,
            "incremento_laboratorio": bool(laboratorio),
            "n_cambios_costo_24m": (h["n_cambios"] if (h := _historia_costo(sc, cod)) else np.nan),
            "delta_ultimo_costo_pct": (round(100 * h["delta_ult"], 1) if h else np.nan),
            "sem_costo_vigente": (h["sem_vigente"] if h else np.nan),
            "fecha_ultimo_cambio_costo": (h["fecha_ult"] if h else None),
            "compramos_tras_cambio": (h["compro"] if h else None),
            "proveedor": provs.get(cod, ""),
            "u_sem_epoca_viva": round(u_sem_era, 1),
            "explicacion": expl,
            "ultima_venta": pd.Timestamp(ult_sem).date().isoformat(),
            "semanas_sin_venta": sem_sin_venta,
            "sem_muertas_stock": sem_muertas,
            "meses_muertos": round(meses_muertos, 1),
            "remate": bool(remate_map.get(cod, False)),
            "clasif_erp": str(clasif_map.get(cod, "")),
            "unidades_12m": round(u12, 0), "unidades_24m": round(u24, 0),
            "precio_epoca_viva": round(precio_era, 2),
            "precio_hoy": round(precio_hoy, 2) if np.isfinite(precio_hoy) else np.nan,
            "delta_precio_pct": round(100 * d_precio, 1) if np.isfinite(d_precio) else np.nan,
            "margen_epoca_viva": round(margen_era, 3) if np.isfinite(margen_era) else np.nan,
            "stock_venta": stock, "reposicion": repo,
            # meses de stock AL RITMO DE SU ÉPOCA VIVA (contra su venta actual
            # sería infinito): si reactivara y vendiera como antes, cuánto dura
            "meses_stock_era": (round(stock / (u_sem_era * 4.345), 1)
                                if u_sem_era > 0 and stock > 0 else np.nan),
            "capital_atrapado": (0 if direc == "VENDE POR PRESENTACIÓN"
                                 else round(stock * costo_era, 0)
                                 if np.isfinite(stock) and stock > 0 else 0),
            "diagnostico": dx,
        })

    df = pd.DataFrame(filas).sort_values("capital_atrapado", ascending=False)
    ruta = os.path.join(BASE, "out", "segunda_capa_dormidos.csv")
    df.to_csv(ruta, index=False)

    print(f"\n== SEGUNDA CAPA: {len(df):,} dormidos diagnosticados "
          f"(+{n_mimc} MI/MC excluidos por invendibles) ==", flush=True)
    print(f"  con venta en 12m (poca): {(df.capa=='12m').sum():,} | "
          f"solo en 24m: {(df.capa=='24m').sum():,}", flush=True)
    print("\n  --- diagnóstico (SKUs | capital atrapado) ---", flush=True)
    g = df.groupby(df.diagnostico.str.split(" (", regex=False).str[0]).agg(
        skus=("codigo", "size"), capital=("capital_atrapado", "sum"))
    for dx, row in g.sort_values("capital", ascending=False).iterrows():
        print(f"  {dx:<38} {row.skus:>6,}  ${row.capital:>12,.0f}", flush=True)
    print(f"\n  capital total atrapado en dormidos con stock: "
          f"${df.capital_atrapado.sum():,.0f}", flush=True)
    caros = df[df.diagnostico.str.startswith('QUEDÓ CARO')]
    if sc is not None and len(caros):
        lab = caros.incremento_laboratorio.sum()
        compro = (caros.compramos_tras_cambio == True).sum()  # noqa: E712
        print(f"\n  --- QUEDÓ CARO: la película de costos (24m) ---", flush=True)
        print(f"  incremento de LABORATORIO (no compramos): {lab} de {len(caros)} | "
              f"sí compramos tras el cambio: {compro}", flush=True)
        vig = caros.sem_costo_vigente.dropna()
        if len(vig):
            print(f"  vigencia del costo actual: mediana {vig.median():.0f} semanas "
                  f"(p25 {vig.quantile(.25):.0f} / p75 {vig.quantile(.75):.0f})", flush=True)
    print(f"\n  --- top 8 'QUEDÓ CARO' por capital atrapado ---", flush=True)
    print(caros.head(8)[["codigo", "ultima_venta", "unidades_24m", "precio_epoca_viva",
                         "precio_hoy", "delta_precio_pct", "stock_venta",
                         "capital_atrapado"]].to_string(index=False), flush=True)
    print(f"\nguardado {ruta}", flush=True)


if __name__ == "__main__":
    correr()
