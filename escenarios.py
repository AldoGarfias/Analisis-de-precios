# -*- coding: utf-8 -*-
"""Simulador de escenarios de precio + recomendación con guardrails.

Responde: ¿cómo se comportarían las VENTAS OBSERVABLES si acepto el precio sugerido?
Compara: (1) mantener, (2) recomendado, (3) rejilla de escenarios adicionales,
con banda de incertidumbre empírica (backtest) y explicación del respaldo.

Insumos (todos generados por el pipeline):
  data/panel.parquet          — panel SKU×semana (panel.py)
  data/elasticidad_sku.parquet— ε global PPML + se (modelo.py)
  data/backtest.parquet       — método u0 ganador + bandas por tercil (validar.py)

Proyección por SKU y factor de precio f = p_nuevo/p_actual:
  unidades(f) = u0 · f^ε         (u0 = pronóstico GBM residual, campeón del backtest)
  neto(f)     = neto0 · f        (supuesto pass-through 1: el neto sigue a la lista;
                                  hallazgo #7 del v2 — se re-mide con datos reales
                                  del ciclo de aprendizaje)
  utilidad(f) = unidades(f) · (neto(f) − costo)

Guardrails (decisiones del proyecto, no re-litigar):
  - paso máx ±4pts POR CICLO DE 3 SEMANAS (el ±10pts ACUMULADO sigue sin
    implementarse — decisión pendiente del usuario, ver CLAUDE.md)
  - piso de margen NETO: costo + 3pts (no bajar si lo rompe)
  - mínimo de actividad para opinar; ABSTENCIÓN = MANTENER a baja confianza
  - sin inventario: NO se afirma stockout; si hay señal de demanda no observada
    (intermitencia alta) la confianza BAJA
  - pct_proyecto alto ⇒ demanda por proyecto (no responde a lista) ⇒ confianza baja

Overrides POST-recomendar() en main(), en este orden (la última palabra):
  0. 🌐 MEZCLA DE CANAL — solo CONFIANZA (media↔alta) en SUBIR ≤4pts según
     %venta en línea (≥70%/≤30%, ambos con ≥10u/26sem); jamás dirección/precio
  1. REMATE MANDA — remate S / R0-R4: dejar agotar, SUBIR/BAJAR ⇒ MANTENER
     (MI/MC = muestras invendibles, excluidos antes de señales)
  2. ⚓ ANCLA DE CANASTA — SUBIR bloqueado si arrastre ≥ 50% de la ganancia
     propia (ε cruzada por bucket de attach; frenos exentos)
  3. KVI — rol de precio frena subidas en la vitrina (dentro de recomendar)
  4. RE-TOQUE — medición abierta (monitoreo) no se re-toca hasta fecha_retoque
     (excepciones: frenar / revertir / sobrestock)
  5. HOLDOUT — semilla = corte: grupo de control estable del ciclo
La referencia completa del árbol: docs/ARQUITECTURA_V3.md.

Salidas locales (fase de pruebas; NO se escribe a la BD):
  out/recomendaciones.csv — 1 fila por SKU: precio actual, sugerido, dirección,
                            confianza, proyección y explicación
  out/escenarios.csv      — 1 fila por SKU×escenario (rejilla completa)
"""
import os

import numpy as np
import pandas as pd

from db import guardar_recos_local

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

CICLO_SEMANAS = 3      # cadencia de sugerencias: cada 3 semanas (regla del negocio:
                       # un precio nunca se mueve dos semanas seguidas)
PASO = 0.04            # paso máx POR CICLO (±4pts) — guardrail: aplicar poco → re-medir
# Rejilla de escenarios. REGLA DE GRANULARIDAD (usuario 2026-07-27): pasos
# enteros de 1% dentro del guardrail (±1..±4) — el ±3 llena el hueco que hacía
# saltar la sugerencia de 2 a 4. NO se afina más (0.5%): la incertidumbre de ε
# y las cubetas de calibración no distinguen medios puntos (falsa precisión).
GRID = [-0.10, -0.08, -0.06, -0.04, -0.03, -0.02, -0.01, 0.0,
        0.01, 0.02, 0.03, 0.04, 0.06, 0.08, 0.10]
PISO_MARGEN = 0.03     # margen neto mínimo tras el cambio
Z95 = 1.96   # nivel de confianza 95% (usuario 2026-07-27; antes IC90/z=1.645)

# umbrales de confianza
MIN_SEM_ALTA = 13      # semanas con venta para confianza alta
MIN_CLI = 3            # clientes distintos promedio/semana
MAX_PROYECTO = 0.30    # >30% líneas de proyecto ⇒ demanda no responde a lista


def cargar():
    pan = pd.read_parquet(os.path.join(DATA, "panel.parquet"))
    # demanda = venta RECURRENTE (sin proyectos); el precio/margen por celda no
    # cambia (viene del total facturado)
    if "unidades_rec" in pan.columns:
        # se conserva la venta TOTAL (incluye proyectos) para meses de stock:
        # el proyecto no es demanda de precio, pero SÍ vacía el almacén
        pan["unidades_tot"] = pan["unidades"].astype(float)
        pan["unidades"] = pan["unidades_rec"].astype(float)
    eps = pd.read_parquet(os.path.join(DATA, "elasticidad_sku.parquet")).iloc[0]
    # bandas: preferir las del GBM (forecast.py) si existen; fallback al backtest
    # de medias simples
    ruta_fb = os.path.join(DATA, "forecast_bandas.parquet")
    bt = (pd.read_parquet(ruta_fb) if os.path.exists(ruta_fb)
          else pd.read_parquet(os.path.join(DATA, "backtest.parquet")))
    # u0 del GBM residual (campeón del backtest 4/4); fallback: media simple
    ruta_u0 = os.path.join(DATA, "forecast_u0.parquet")
    u0_gbm = (pd.read_parquet(ruta_u0).set_index("codigo").u0
              if os.path.exists(ruta_u0) else None)
    # ε por SKU (la de su segmento de rotación; modelo.py). Si no existe el
    # archivo (corrida vieja), todos usan la global.
    ruta_eps = os.path.join(DATA, "eps_por_sku.parquet")
    eps_sku = pd.read_parquet(ruta_eps).set_index("codigo") if os.path.exists(ruta_eps) else None
    # existencias semanales (valor_inventario agregada); opcional
    ruta_ex = os.path.join(DATA, "reporte61", "existencias_sem.parquet")
    exist = pd.read_parquet(ruta_ex) if os.path.exists(ruta_ex) else None
    # guard anti-snapshot-parcial: si hay re-extracción en curso, usar el
    # archivo más COMPLETO (backup vs actual)
    ruta_bak = os.path.join(DATA, "reporte61", "existencias_sem_backup.parquet")
    if exist is not None and os.path.exists(ruta_bak):
        bak = pd.read_parquet(ruta_bak)
        if bak.semana.max() > exist.semana.max():
            exist = bak
    return pan[pan.activo].copy(), eps, bt, eps_sku, exist, u0_gbm


_SEM_METODO = {"ultima_sem": 1, "media_4s": 4, "media_8s": 8, "media_12s": 12}


def estado_actual(pan, metodo_u0="media_8s"):
    """Foto por SKU: u0 (método GANADOR del backtest), precio vigente, señales."""
    pan = pan.sort_values("semana")
    semanas = np.sort(pan.semana.unique())
    n_u0 = _SEM_METODO[metodo_u0]
    ultN = semanas[-n_u0:]
    # u0 sobre matriz con ceros (semana activa sin venta = 0 observado)
    m = pan.pivot_table(index="codigo", columns="semana", values="unidades",
                        aggfunc="sum").reindex(columns=semanas).fillna(0.0)
    u0 = m[ultN].mean(axis=1).rename("u0")

    rec = pan[pan.semana.isin(semanas[-8:])].groupby("codigo").agg(
        neto0=("neto_prom", "median"),
        costo=("costo_prom", "median"),
        n_clientes=("n_clientes", "mean"),
        pct_proyecto=("pct_proyecto", "mean"),
        d_dos=("d_dos_prom", "median"),
        tipo_precio=("tipo_precio", lambda s: s.mode().iat[0]),
    )
    # precio ACTUAL = lista de la última semana observada del SKU (no mediana:
    # un cambio reciente de lista debe reflejarse como punto de partida).
    rec["precio_actual"] = pan.groupby("codigo").tail(1).set_index("codigo").precio_lista
    hist = pan.groupby("codigo").agg(
        sem_con_venta=("semana", "nunique"),
        var_precio=("precio_lista", lambda s: s.std() / max(s.median(), 1e-9)),
    )
    est = rec.join(u0).join(hist).dropna(subset=["u0", "precio_actual", "costo"])
    est = est[est.u0 > 0]
    return est


def confianza(est):
    """alta / media / baja según respaldo real. Baja ⇒ abstención (MANTENER).

    T1.5 (aprobado 2026-07-27): la CLASE DE SERIE (Syntetos-Boylan, ADI×CV²,
    data/adi_cv2.parquet) entra a la escalera — una serie GRUMOSA (lumpy:
    compra esporádica Y de tamaño impredecible) no puede ser confianza ALTA:
    su pronóstico puntual no da para esa seguridad (el WAPE en lumpy premia
    pronosticar cero; ver docs/BENCHMARK_MEJORES_PRACTICAS.md).
    """
    señales = pd.DataFrame(index=est.index)
    señales["historia"] = est.sem_con_venta >= MIN_SEM_ALTA
    señales["clientes"] = est.n_clientes >= MIN_CLI
    señales["hay_var_precio"] = est.var_precio > 1e-3
    señales["no_proyecto"] = est.pct_proyecto <= MAX_PROYECTO
    # intermitencia = señal de posible demanda no observada ⇒ resta
    pts = señales.sum(axis=1)
    conf = pd.Series("baja", index=est.index)
    conf[pts >= 3] = "media"
    conf[pts == 4] = "alta"
    ruta_c = os.path.join(DATA, "adi_cv2.parquet")
    if os.path.exists(ruta_c):
        clase = pd.read_parquet(ruta_c).set_index("codigo").clase
        est["clase_serie"] = clase.reindex(est.index).fillna("sin clase")
        lumpy = (est.clase_serie == "grumosa (lumpy)") & (conf == "alta")
        conf[lumpy] = "media"
        print(f"  clase de serie: {est.clase_serie.value_counts().to_dict()} | "
              f"tope lumpy alta→media: {int(lumpy.sum())} SKUs", flush=True)
    else:
        est["clase_serie"] = "sin clase"
    return conf, señales


def _proyecta(u0, neto0, costo, eps, f):
    u = u0 * f ** eps
    return u, u * (neto0 * f - costo)


def meses_stock(pan, exist, est):
    """Meses de stock v3 (usuario 2026-07-31: "apliquemos esto nuevo") —
    stock de VENTA ÷ demanda esperada mensual, con filtro de credibilidad:

      1. FORECAST PROPIO (ensamble GBM+ingenuo 50/50, adoptado por duelo OOT;
         promedio de los meses FUTUROS del archivo mensual más reciente de
         forecast_mensual.py) cuando HACE SENTIDO para ese SKU: entre ⅓× y 3×
         de la vía 2. El filtro es imprescindible: el ancla de ingenuo hereda
         el eco de un pico de proyecto (caso TXTPH700C).
      2. VÍA PROPIA (respaldo y vara del filtro):
         recurrente de los últimos 6 meses cerrados EXCLUYENDO meses de
         cero-por-stockout (venta ≈0 Y ≥50% de sus semanas sin stock vendible;
         mínimo 2 meses válidos; producto nuevo usa solo meses desde su
         primera existencia) + TASA DE PROYECTOS de la ventana completa
         (el flujo raro se promedia a ventana larga, no se embarra al mes).
      3. Sin demanda por ninguna vía y con stock ⇒ 99 (sobrestock). NOTA
         deliberada (N6): sin vía propia > 0 no hay vara para el filtro, así
         que el forecast NO rescata a un SKU muerto — conservador a propósito.

    Devuelve (meses, fuente); fuente ∈ {forecast propio (ensamble), venta
    propia+proyectos, sin demanda}. AWS ya no participa aquí (queda como 2ª
    opinión aws_* y voz del examen). Las señales de PRECIO siguen sobre
    recurrente, sin cambio.
    """
    col = ("disp_venta" if "disp_venta" in exist.columns else
           ("disponible" if "disponible" in exist.columns else "existencia"))
    sem_ex = np.sort(exist.semana.unique())
    pv = (exist.pivot_table(index="codigo", columns="semana", values=col, aggfunc="last")
          .reindex(columns=sem_ex))
    stock_hoy = pv.iloc[:, -1]
    con_stock = pv.gt(0).values
    first = con_stock.argmax(axis=1)
    tiene = con_stock.any(axis=1)

    # --- vía 2a: recurrente mensual sin ceros-por-stockout ---
    p = pan.copy()
    p["_mes"] = pd.to_datetime(p.semana).dt.to_period("M")
    # ancla al CORTE DEL PANEL, no al reloj (auditoría 2026-07-31, N7: con
    # panel atrasado, hoy() metería un mes sub-cubierto lleno de ceros)
    mes_hoy = (pd.Timestamp(pan.semana.max()) + pd.Timedelta(days=6)).to_period("M")
    meses6 = [mes_hoy - k for k in range(1, 7)]
    vm = (p[p._mes.isin(meses6)].groupby(["codigo", "_mes"]).unidades.sum()
          .unstack().reindex(columns=meses6).fillna(0.0))
    e = exist.copy()
    e["_mes"] = pd.to_datetime(e.semana).dt.to_period("M")
    frac_ss = (e[e._mes.isin(meses6)].assign(_sin=lambda d: d[col] <= 0)
               .groupby(["codigo", "_mes"])._sin.mean()
               .unstack().reindex(columns=meses6).fillna(0.0))
    # mes de primera existencia (producto nuevo no promedia meses pre-vida)
    primera_mes = pd.Series(pd.to_datetime(sem_ex[first]), index=pv.index).dt.to_period("M")

    # --- vía 2b: tasa de proyectos, ventana completa ---
    n_meses_ventana = max(p._mes.nunique(), 1)
    if "unidades_tot" in p.columns:
        proy_rate = ((p.unidades_tot - p.unidades).clip(lower=0)
                     .groupby(p.codigo).sum() / n_meses_ventana)
    else:
        proy_rate = pd.Series(0.0, index=est.index)

    # --- vía 1 (v3, usuario 2026-07-31: "apliquemos esto nuevo"): el ENSAMBLE
    # PROPIO mensual (GBM+ingenuo 50/50, ganador 6/6 del duelo OOT sobre venta
    # TOTAL — exactamente el target de esta métrica) sustituye a AWS como
    # primario. El FILTRO DE CREDIBILIDAD se conserva: el ancla de ingenuo del
    # ensamble hereda su debilidad tras un pico de proyecto (TXTPH700C:
    # pronosticaría ~90/mes tras un one-off de 154) y el filtro lo regresa a
    # la vía propia. AWS queda como segunda opinión informativa (aws_*).
    import glob as _glob
    fc5 = pd.Series(dtype=float)
    archivos = sorted(_glob.glob(os.path.join(DATA, "forecast_mensual_propio",
                                              "pred_*.parquet")), reverse=True)
    if archivos:
        pr = pd.read_parquet(archivos[0])
        fut = pr[pr.mes > mes_hoy.strftime("%Y-%m")]
        fc5 = fut.groupby("codigo").pred.mean().clip(lower=0)
        if pr.mes.min() < mes_hoy.strftime("%Y-%m"):
            print(f"  (aviso: el archivo del forecast propio es de "
                  f"{os.path.basename(archivos[0])[5:11]} — el cron lo renueva "
                  f"los primeros días del mes)", flush=True)

    out = pd.Series(np.nan, index=est.index)
    fuente = pd.Series("", index=est.index)
    for cod in est.index:
        if cod not in pv.index:
            continue
        i = pv.index.get_loc(cod)
        if not tiene[i]:
            continue
        s = float(stock_hoy.iloc[i]) if np.isfinite(stock_hoy.iloc[i]) else 0.0
        # vía propia
        propia = 0.0
        if cod in vm.index:
            v = vm.loc[cod]
            f_ss = frac_ss.loc[cod] if cod in frac_ss.index else pd.Series(0.0, index=meses6)
            pmes = primera_mes.iloc[i]
            validos = [m for m in meses6
                       if m >= pmes and not (v[m] <= 1 and f_ss[m] >= 0.5)]
            if len(validos) >= 2:
                propia = float(v[validos].mean())
            elif len(validos) == 1:
                propia = float(v[validos[0]])
        propia += float(proy_rate.get(cod, 0.0))
        # ¿el ensamble hace sentido para ESTE SKU? (mismo filtro que tuvo AWS)
        f5 = float(fc5.get(cod, np.nan))
        usa_fc = (np.isfinite(f5) and f5 > 0 and propia > 0
                  and (1 / 3) <= f5 / propia <= 3)
        demanda = f5 if usa_fc else propia
        if demanda <= 0:
            out[cod] = 99.0 if s > 0 else np.nan
            fuente[cod] = "sin demanda"
        else:
            out[cod] = s / demanda
            fuente[cod] = ("forecast propio (ensamble)" if usa_fc
                           else "venta propia+proyectos")
    n_fc = int((fuente == "forecast propio (ensamble)").sum())
    n_pro = int((fuente == "venta propia+proyectos").sum())
    print(f"  meses de stock v3: {n_fc:,} con forecast propio (pasó el filtro) | "
          f"{n_pro:,} con venta propia+proyectos | "
          f"{int((fuente == 'sin demanda').sum()):,} sin demanda", flush=True)
    return out, fuente


def rol_precio(est):
    """Rol de precio del SKU (adaptación B2B del article segmentation
    KVI/SD/PG de los suites comerciales), con datos propios:

      - visibilidad  = rank de (ingreso semanal × clientes/sem): lo que muchos
                       clientes compran seguido forma la imagen de precio.
      - sensibilidad = evidencia dura (cayó tras aumento) + profundidad del
                       descuento que exige el mercado (d_dos alto = peleado).

      KVI            visible y sensible  → PROTEGER precio (imagen)
      Sales Driver   visible, poco sensible → precio de mercado, pasos normales
      Profit Gen     poco visible e insensible → margen libre
      Estándar       el resto
    """
    ingreso = (est.u0 * est.neto0).rank(pct=True)
    clientes = est.n_clientes.rank(pct=True)
    vis = 0.5 * ingreso + 0.5 * clientes
    d_rank = est.d_dos.rank(pct=True)
    # sensible = evidencia dura (cayó tras aumento) O el mercado le exige
    # descuentos profundos vs pares (top 20%); insensible = ni lo uno ni lo otro
    # y descuento por debajo de la mediana
    sens_alta = est.cayo_tras_aumento.astype(bool) | (d_rank >= 0.80)
    sens_baja = ~est.cayo_tras_aumento.astype(bool) & (d_rank <= 0.50)
    rol = pd.Series("Estándar", index=est.index)
    rol[(vis < 0.80) & sens_baja] = "Profit Gen"
    rol[(vis >= 0.80) & ~sens_alta] = "Sales Driver"
    rol[(vis >= 0.80) & sens_alta] = "KVI"
    return rol


def metricas_dinamica(pan, est):
    """Dinámica de demanda por SKU (venta RECURRENTE, ajustada por mercado):

      - crecimiento MES vs MES (patrón, no salto): cadena de comparaciones
        mensuales — abr/mar, may/abr, jun/may... — sobre los últimos 6 meses
        COMPLETOS (mínimo 4), cada paso ajustado por mercado.
          · crecimiento = MEDIANA de los pasos (robusta: un mes pico no engaña)
          · meses_alza  = cuántos pasos fueron positivos (consistencia: "4/5")
        Solo meses cerrados (hay picos en cierres de semana/mes); el mes en
        curso NUNCA entra. Base mínima 10 uds/mes para que un paso cuente.
      - cobertura_sem: semanas de stock en almacenes de venta al ritmo de las
        últimas 4 semanas. Cobertura corta + demanda acelerando ⇒ subir para
        FRENAR la venta mientras se reabastece.
    """
    semanas = np.sort(pan.semana.unique())
    df = pan[["codigo", "semana", "unidades"]].copy()
    # mes del punto medio de la semana (lunes+3): asigna cada semana al mes
    # donde cae la mayoría de sus días
    df["mes"] = (df.semana + pd.Timedelta(days=3)).dt.to_period("M")
    mes_en_curso = (pd.Timestamp(semanas[-1]) + pd.Timedelta(days=3)).to_period("M")
    df = df[df.mes < mes_en_curso]          # solo meses COMPLETOS
    meses = np.sort(df.mes.unique())        # TODOS los meses completos (hasta 24)
    piv = (df.pivot_table(index="codigo", columns="mes", values="unidades",
                          aggfunc="sum").reindex(columns=meses).fillna(0.0))

    def _patron(n_pasos, min_validos):
        """Mediana y racha de la cadena mes-vs-mes de los últimos n_pasos."""
        cols = list(meses[-(n_pasos + 1):])
        P = piv[cols].values
        mkt = P.sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            f_mkt = mkt[1:] / np.maximum(mkt[:-1], 1e-9)
            pasos = (P[:, 1:] / np.maximum(P[:, :-1], 1e-9)) / f_mkt - 1
        pasos = np.where(P[:, :-1] >= 10, pasos, np.nan)   # base mínima por paso
        n_valid = np.isfinite(pasos).sum(axis=1)
        with np.errstate(all="ignore"):
            mediana = np.nanmedian(np.where(np.isfinite(pasos), pasos, np.nan), axis=1)
        n_alza = (np.where(np.isfinite(pasos), pasos, -1) > 0).sum(axis=1)
        crec_s = pd.Series(np.where(n_valid >= min_validos, mediana, np.nan), index=piv.index)
        alza_s = pd.Series(np.where(n_valid >= min_validos, n_alza / np.maximum(n_valid, 1),
                                    np.nan), index=piv.index)
        racha_s = pd.Series([f"{a}/{v}" if v >= min_validos else ""
                             for a, v in zip(n_alza, n_valid)], index=piv.index)
        return crec_s, alza_s, racha_s

    # 6 meses (la señal operativa) + horizontes largos para confirmar patrones
    # (regla del negocio: en KVIs no basta lo reciente — 12m, y si no
    # concluye, toda la historia disponible)
    crec, alza_frac, racha = _patron(min(6, len(meses) - 1), 3)
    crec12, alza12, racha12 = _patron(min(12, len(meses) - 1), 8)
    crec24, alza24, racha24 = _patron(len(meses) - 1, 14)

    uv = (pan.pivot_table(index="codigo", columns="semana", values="unidades",
                          aggfunc="sum").reindex(columns=semanas).fillna(0.0))
    v4 = uv.iloc[:, -4:].mean(axis=1)
    cobertura = est.disponible / v4.reindex(est.index).clip(lower=1e-9)
    return pd.DataFrame({"crecimiento": crec.reindex(est.index),
                         "alza_frac": alza_frac.reindex(est.index),
                         "meses_alza": racha.reindex(est.index).fillna(""),
                         "crecimiento_12m": crec12.reindex(est.index),
                         "alza_frac_12m": alza12.reindex(est.index),
                         "meses_alza_12m": racha12.reindex(est.index).fillna(""),
                         "crecimiento_24m": crec24.reindex(est.index),
                         "alza_frac_24m": alza24.reindex(est.index),
                         "meses_alza_24m": racha24.reindex(est.index).fillna(""),
                         "cobertura_sem": cobertura})


def señales_revision(pan, est, exist=None):
    """Dos señales por SKU que BLOQUEAN el SUBIR (la evidencia propia le gana
    al promedio del segmento):

    1. cayo_tras_aumento: tras la última subida de lista ≥2%, el volumen cayó
       ≥25% MÁS que el mercado en las mismas semanas.
    2. no_rota: margen alto vs pares (cuartil superior) + caída sostenida de su
       propia venta ≥30% ajustada por mercado. Heurística honesta: sin precios de
       competencia no prueba que esté caro; lo SEÑALA usando su historia. Cuando
       haya acceso a stock, se confirmará contra disponibilidad.
    Ambas se aplican solo con historia suficiente (el gate de confianza ya
    exige semanas/clientes; aquí además ≥4 sem antes y ≥3 después del cambio).
    """
    semanas = np.sort(pan.semana.unique())
    mkt = pan.groupby("semana").unidades.sum().reindex(semanas)
    u = pan.pivot(index="codigo", columns="semana", values="unidades").reindex(columns=semanas).fillna(0.0)
    p = pan.pivot(index="codigo", columns="semana", values="precio_lista").reindex(columns=semanas).ffill(axis=1)
    cst = pan.pivot(index="codigo", columns="semana", values="costo_prom").reindex(columns=semanas).ffill(axis=1)
    # stock DISPONIBLE semanal (si hay datos) para descartar caídas por
    # DISPONIBILIDAD (parquets viejos solo traen existencia total: fallback)
    ex = None
    if exist is not None:
        col_disp = ("disp_venta" if "disp_venta" in exist.columns else
                    ("disponible" if "disponible" in exist.columns else "existencia"))
        ex = (exist.pivot_table(index="codigo", columns="semana", values=col_disp,
                                aggfunc="last").reindex(columns=semanas).ffill(axis=1))

    # NO revertir los FRENOS del propio motor (auditoría 2026-07-30, C2): un
    # freno que funciona TIRA la venta a propósito — leerlo como "aumento
    # dañino" desharía el freno mientras el seguimiento diario dice sostenerlo.
    # La re-decisión de un freno pertenece a seguimiento_frenos (cobertura ≥6
    # sem), no a estas señales.
    ruta_fren = os.path.join(os.path.dirname(DATA), "out", "seguimiento_frenos.csv")
    frenados = set()
    if os.path.exists(ruta_fren):
        fr = pd.read_csv(ruta_fren)
        if {"tipo", "estado", "codigo"}.issubset(fr.columns):
            # solo frenos ACTIVOS (auditoría 2026-07-31, N5: 'estado != cerrado'
            # excluía para SIEMPRE — reabastecido ya fue re-decidido y las
            # señales del motor deben volver a aplicarle)
            frenados = set(fr[(fr.tipo == "freno")
                              & (fr.estado == "esperando_reposicion")]
                           .codigo.astype(str))
    if frenados:
        print(f"  señales de revisión: {len(frenados)} SKUs con freno activo "
              f"excluidos (C2: el motor no revierte sus propios frenos)", flush=True)

    cayo = pd.Series(False, index=est.index)
    precio_pre = pd.Series(np.nan, index=est.index)
    for cod in est.index:
        if cod not in u.index or cod in frenados:
            continue
        pr, uv = p.loc[cod].values, u.loc[cod].values
        idx = None
        for i in range(1, len(pr)):
            if pr[i - 1] > 0 and np.isfinite(pr[i]) and pr[i] / pr[i - 1] - 1 >= 0.02:
                idx = i
        if idx is None or idx < 4 or idx > len(pr) - 3:
            continue
        # RESPETAR aumentos que vienen de COSTO: si el costo subió al menos la
        # mitad de lo que subió el precio, el aumento es pass-through legítimo
        # ⇒ no se marca como dañino ni se sugiere revertirlo.
        if cod in cst.index:
            cs = cst.loc[cod].values
            c_pre = np.nanmedian(cs[max(0, idx - 4):idx])
            c_post = np.nanmedian(cs[idx:idx + 4])
            if (np.isfinite(c_pre) and c_pre > 0 and np.isfinite(c_post)
                    and (c_post / c_pre - 1) >= 0.5 * (pr[idx] / pr[idx - 1] - 1)):
                continue
        antes, despues = uv[max(0, idx - 8):idx].mean(), uv[idx:].mean()
        m_antes, m_desp = mkt.iloc[max(0, idx - 8):idx].mean(), mkt.iloc[idx:].mean()
        if antes <= 0 or m_antes <= 0 or m_desp <= 0:
            continue
        # con datos de stock: si tras el aumento el DISPONIBLE promedio era menor
        # a ~1 semana de la venta previa, la caída se explica por disponibilidad,
        # no por precio ⇒ no señalar
        if ex is not None and cod in ex.index:
            ex_desp = ex.loc[cod].values[idx:]
            ex_desp = ex_desp[np.isfinite(ex_desp)]
            if len(ex_desp) and np.nanmean(ex_desp) < max(antes, 1):
                continue
        rel = (despues / antes) / (m_desp / m_antes) - 1
        if rel <= -0.25:
            cayo[cod] = True
            precio_pre[cod] = pr[idx - 1]  # nivel previo al aumento dañino

    margen = (est.neto0 - est.costo) / est.neto0
    m_alto = margen >= margen.quantile(0.75)
    k = max(4, len(semanas) // 3)
    prim, ult = u.iloc[:, :k].mean(axis=1), u.iloc[:, -k:].mean(axis=1)
    caida_rel = (ult / prim.clip(lower=1e-9)) / (mkt.iloc[-k:].mean() / mkt.iloc[:k].mean()) - 1
    no_rota = (m_alto & (caida_rel.reindex(est.index) <= -0.30)
               & (prim.reindex(est.index) > 0)).fillna(False)
    # con stock: "no rota" solo si HOY tiene existencia (si no hay stock, no
    # rota porque no hay qué vender — es disponibilidad, no precio)
    if "existencia" in est.columns and est.existencia.notna().any():
        no_rota = no_rota & (est.existencia > 0)
    # los frenos activos tampoco cuentan como "no rota": su caída es inducida
    no_rota[no_rota.index.isin(frenados)] = False
    return cayo, no_rota, precio_pre


def recomendar(est, bandas):
    """Dirección por SKU con SU ε (segmento) y overrides de evidencia propia."""
    conf, señales = confianza(est)
    eps_v = est.eps.values
    eps_lo_v = est.eps.values - Z95 * est.se.values
    eps_hi_v = est.eps.values + Z95 * est.se.values

    # dirección por UTILIDAD: signo de dπ/df en f=1 con la ε del SKU. Equivale
    # a comparar ε contra el punto de quiebre económico ε* = −1/margen_neto.
    # Con demanda inelástica (|ε|<1) esto casi siempre da SUBIR.
    dpi = est.u0 * (est.neto0 * (1 + eps_v) - eps_v * est.costo)
    f_reco = np.where(dpi > 0, 1 + PASO, 1 - PASO)

    # --- políticas BAJAR (objetivos del negocio más allá de la utilidad/sem) ---
    # 1) REVERTIR AUMENTO DAÑINO: si la evidencia propia muestra que la última
    #    subida tiró la venta (ajustada por mercado, con stock disponible),
    #    bajar HACIA el nivel previo, a lo más −PASO por mes.
    cayo_v = est.cayo_tras_aumento.values.astype(bool)
    rf = (est.precio_pre_aumento / est.precio_actual).values
    rf = np.where(np.isfinite(rf) & (rf < 1), np.maximum(rf, 1 - PASO), 1.0)
    f_reco = np.where(cayo_v, np.minimum(f_reco, rf), f_reco)
    # 2) SOBRESTOCK (≥12 meses): el objetivo pasa de utilidad a ROTACIÓN de
    #    capital ⇒ bajar −PASO (nunca subir), si el piso de margen lo permite.
    sobrestock = (est.meses_inv >= 12).fillna(False).values
    f_reco = np.where(sobrestock & ~cayo_v, np.minimum(f_reco, 1 - PASO), f_reco)
    # 3) VENDIENDO CARO / NO ROTA: margen alto vs pares + venta cayendo con
    #    stock disponible ⇒ el precio puede estar mal calculado. BAJAR para
    #    rotar el producto (importa la utilidad, pero también rotar).
    no_rota_v = est.no_rota.values.astype(bool)
    f_reco = np.where(no_rota_v & ~cayo_v & ~sobrestock,
                      np.minimum(f_reco, 1 - PASO), f_reco)
    # 4) FRENAR DURANTE REABASTO: patrón de demanda creciendo (mediana mes-vs-mes
    #    ≥ +7% Y mayoría de meses al alza) con cobertura corta (<4 semanas)
    #    ⇒ SUBIR para desacelerar la venta mientras llega la reposición.
    frenar = ((est.crecimiento >= 0.07) & (est.alza_frac >= 0.6)
              & (est.cobertura_sem < 4)).fillna(False).values
    f_reco = np.where(frenar & ~cayo_v & ~sobrestock & ~no_rota_v, 1 + PASO, f_reco)
    # 5) POLÍTICA KVI (aprobada 2026-07-27, punto medio con horizontes largos):
    #    los KVI forman la imagen de precio ⇒ NO suben salvo (a) freno por
    #    reabasto (exención F5), o (b) demanda creciendo CONFIRMADA en
    #    horizonte largo: señal de 6m Y confirmación a 12m (mediana ≥+3%/mes,
    #    ≥55% de meses al alza); si 12m no es concluyente (historia corta),
    #    se escala a 24m — no basta lo reciente, se usa toda la información.
    kvi = (est.rol == "KVI").values if "rol" in est.columns else np.zeros(len(est), bool)
    señal_6m = ((est.crecimiento >= 0.07) & (est.alza_frac >= 0.6)).fillna(False).values
    ok12 = ((est.crecimiento_12m >= 0.03) & (est.alza_frac_12m >= 0.55)).fillna(False).values
    valido12 = est.crecimiento_12m.notna().values
    ok24 = ((est.crecimiento_24m >= 0.03) & (est.alza_frac_24m >= 0.55)).fillna(False).values
    demanda_confirmada = np.where(valido12, ok12, ok24)
    excepcion_kvi = frenar | (señal_6m & demanda_confirmada)
    kvi_bloqueado = kvi & (f_reco > 1) & ~excepcion_kvi
    f_reco = np.where(kvi_bloqueado, 1.0, f_reco)

    # piso de margen para CUALQUIER bajada: costo + 3pts
    margen_f = (est.neto0 * f_reco - est.costo) / (est.neto0 * f_reco)
    f_reco = np.where((f_reco < 1) & (margen_f < PISO_MARGEN), 1.0, f_reco)
    # abstención: confianza baja ⇒ mantener
    f_reco = np.where(conf == "baja", 1.0, f_reco)

    tercil = pd.qcut(est.u0, 3, labels=["bajo", "medio", "alto"])
    bt = bandas.set_index("tercil")

    filas, esc_filas = [], []
    for i, (cod, r) in enumerate(est.iterrows()):
        f = float(f_reco[i])
        b = bt.loc[str(tercil.iloc[i])]
        e_c, e_lo, e_hi = float(eps_v[i]), float(eps_lo_v[i]), float(eps_hi_v[i])
        u_reco, pi_reco = _proyecta(r.u0, r.neto0, r.costo, e_c, f)
        _, pi_hold = _proyecta(r.u0, r.neto0, r.costo, e_c, 1.0)
        # banda: incertidumbre de pronóstico × incertidumbre de ε.
        # T2.2: banda CONDICIONAL por SKU (cuantil) donde existe (suave/lumpy);
        # fallback a la banda del tercil (errática/intermitente/sin clase)
        tiene_q = ("u_p10" in est.columns and np.isfinite(r.get("u_p10", np.nan))
                   and np.isfinite(r.get("u_p90", np.nan)))
        blo = float(r.u_p10) if tiene_q else r.u0 * b.p10
        bhi = float(r.u_p90) if tiene_q else r.u0 * b.p90
        u_lo = blo * f ** (e_hi if f > 1 else e_lo)
        u_hi = bhi * f ** (e_lo if f > 1 else e_hi)
        dir_ = "MANTENER" if f == 1.0 else ("SUBIR" if f > 1 else "BAJAR")
        m_inv = est.meses_inv.iloc[i] if "meses_inv" in est.columns else np.nan
        if bool(est.cayo_tras_aumento.iloc[i]):
            motivo_rev = "revertir aumento dañino (la venta cayó vs mercado, sin causa de costo)"
        elif bool(sobrestock[i]):
            motivo_rev = f"sobrestock ({m_inv:.0f} meses) — acelerar rotación"
        elif bool(est.no_rota.iloc[i]):
            # misma señal, dos lecturas: con volumen alto el problema no es que
            # "no se venda", es que PIERDE PARTICIPACIÓN con margen de los caros
            motivo_rev = ("vendiendo caro — pierde participación vs mercado; bajar para recuperar"
                          if r.u0 >= 50 else
                          "vendiendo caro, no se vende — bajar para rotar")
        elif bool(frenar[i]):
            # cruce abstención×freno (auditoría 2026-07-27): si la confianza es
            # baja, la ABSTENCIÓN manda — el motivo NO debe decir "frenar" (el
            # badge y el seguimiento diario tratarían como freno algo no aplicado)
            if conf.iloc[i] == "baja":
                motivo_rev = ("señal de freno (demanda creciendo, stock corto) — "
                              "evidencia insuficiente ⇒ abstención")
            else:
                motivo_rev = (f"demanda creciendo {100*est.crecimiento.iloc[i]:+.0f}%/mes "
                              f"({est.meses_alza.iloc[i]} meses al alza) con "
                              f"{est.cobertura_sem.iloc[i]:.1f} sem de stock — frenar durante reabasto")
        elif bool(kvi_bloqueado[i]):
            r12 = est.meses_alza_12m.iloc[i] or "sin datos"
            motivo_rev = (f"KVI: proteger imagen de precio — demanda no confirmada en "
                          f"horizonte largo (12m: {r12} meses al alza)")
        else:
            motivo_rev = ""
        exp = (f"{int(r.sem_con_venta)} sem con venta, {r.n_clientes:.0f} cli/sem, "
               f"{'con' if señales.hay_var_precio.iloc[i] else 'sin'} variación de lista; "
               f"precisión del pronóstico: "
               f"la venta real suele quedar entre {100*blo/max(r.u0,1e-9):.0f}% y {100*bhi/max(r.u0,1e-9):.0f}% de lo pronosticado{' (a la medida del modelo)' if tiene_q else ' (promedio de su grupo)'}; elasticidad {e_c:.2f}±{Z95*float(est.se.iloc[i]):.2f} "
               f"(segmento {est.segmento.iloc[i]})"
               + (f"; REVISAR: {motivo_rev}" if motivo_rev else ""))
        filas.append({
            "codigo": cod, "tipo_precio": int(r.tipo_precio),
            "precio_actual": round(r.precio_actual, 2),
            "precio_sugerido": round(r.precio_actual * f, 2),
            "cambio_pct": round(100 * (f - 1), 1), "direccion": dir_,
            "revisar": motivo_rev,
            "rol": est.rol.iloc[i] if "rol" in est.columns else "",
            "proveedor": est.proveedor.iloc[i] if "proveedor" in est.columns else "",
            "segmento": est.segmento.iloc[i],
            "clase_serie": (est.clase_serie.iloc[i] if "clase_serie" in est.columns
                            else "sin clase"),
            "eps": round(e_c, 3),
            "crecimiento": (round(float(est.crecimiento.iloc[i]), 3)
                            if "crecimiento" in est.columns and np.isfinite(est.crecimiento.iloc[i]) else np.nan),
            "meses_alza": est.meses_alza.iloc[i] if "meses_alza" in est.columns else "",
            "n_clientes": round(float(est.n_clientes.iloc[i]), 1),
            "cobertura_sem": (round(float(est.cobertura_sem.iloc[i]), 1)
                              if "cobertura_sem" in est.columns and np.isfinite(est.cobertura_sem.iloc[i]) else np.nan),
            "existencia": round(float(est.existencia.iloc[i]), 0) if "existencia" in est.columns else np.nan,
            "disponible": round(float(est.disponible.iloc[i]), 0) if "disponible" in est.columns else np.nan,
            "reposicion": round(float(est.backorder.iloc[i]), 0) if "backorder" in est.columns else np.nan,
            "meses_inv": round(float(m_inv), 1) if np.isfinite(m_inv) else np.nan,
            "meses_fuente": (str(est.meses_fuente.iloc[i])
                             if "meses_fuente" in est.columns else ""),
            "confianza": conf.iloc[i],
            "u_sem_actual": round(r.u0, 1),
            "u_sem_proyectado": round(u_reco, 1),
            "u_sem_p10": round(u_lo, 1), "u_sem_p90": round(u_hi, 1),
            "utilidad_sem_mantener": round(pi_hold, 0),
            "utilidad_sem_sugerido": round(pi_reco, 0),
            "margen_actual": round((r.neto0 - r.costo) / r.neto0, 3),
            "explicacion": exp,
        })
        for g in GRID:
            f2 = 1 + g
            u_g, pi_g = _proyecta(r.u0, r.neto0, r.costo, e_c, f2)
            # rango de unidades: error de pronóstico (backtest) × extremo de ε
            ug_lo = blo * f2 ** (e_hi if f2 > 1 else e_lo)
            ug_hi = bhi * f2 ** (e_lo if f2 > 1 else e_hi)
            # Δ utilidad vs mantener con IC95 por ε. El error de pronóstico escala
            # a AMBOS escenarios por igual (se cancela en la comparación); la
            # factibilidad de la DECISIÓN la gobierna la incertidumbre de ε.
            m0 = r.neto0 - r.costo
            m1 = r.neto0 * f2 - r.costo
            d_eps = [r.u0 * (f2 ** e * m1 - m0) for e in (e_lo, e_hi)]
            du_lo, du_hi = min(d_eps), max(d_eps)
            # elasticidad de EQUILIBRIO: la ε que dejaría Δ=0 para este escenario.
            # Subir gana si ε_real > ε_eq; bajar gana si ε_real < ε_eq. Comparar
            # contra el IC95 medido dice qué tan lejos está el punto de quiebre.
            eps_eq = (float(np.log(m0 / m1) / np.log(f2))
                      if (g != 0 and m0 > 0 and m1 > 0) else np.nan)
            esc_filas.append({
                "codigo": cod, "escenario_pct": round(100 * g, 0),
                "precio": round(r.precio_actual * f2, 2),
                "unidades_sem": round(u_g, 1),
                "unidades_p10": round(ug_lo, 1), "unidades_p90": round(ug_hi, 1),
                "utilidad_sem": round(pi_g, 0),
                "d_util_vs_mantener": round(u_g * (r.neto0 * f2 - r.costo) - r.u0 * m0, 0),
                "d_util_lo": round(du_lo, 0), "d_util_hi": round(du_hi, 0),
                "eps_equilibrio": round(eps_eq, 2) if np.isfinite(eps_eq) else None,
                "decision_robusta": bool(du_lo > 0) if g > 0 else (bool(du_hi < 0) if g < 0 else None),
                "margen": round((r.neto0 * f2 - r.costo) / (r.neto0 * f2), 3),
            })
    return pd.DataFrame(filas), pd.DataFrame(esc_filas)


def main():
    pan, eps, bandas, eps_sku, exist, u0_gbm = cargar()
    eps_g, se_g = float(eps.eps_global), float(eps.se_global)
    metodo = bandas.metodo_u0.iloc[0]
    est = estado_actual(pan, metodo_u0=metodo if metodo in _SEM_METODO else "media_4s")
    # u0 oficial = GBM residual donde exista (campeón validado); el resto
    # conserva la media simple como fallback
    if u0_gbm is not None:
        g = u0_gbm.reindex(est.index)
        est.loc[g.notna() & (g > 0), "u0"] = g[g.notna() & (g > 0)]
    # T2.2: bandas condicionales por SKU (si forecast.py las generó)
    ruta_u0 = os.path.join(DATA, "forecast_u0.parquet")
    if os.path.exists(ruta_u0):
        u0df = pd.read_parquet(ruta_u0).set_index("codigo")
        if "u_p10" in u0df.columns:
            est["u_p10"] = u0df.u_p10.reindex(est.index)
            est["u_p90"] = u0df.u_p90.reindex(est.index)
            print(f"  bandas por SKU (cuantil): "
                  f"{int(est.u_p10.notna().sum()):,} SKUs", flush=True)
    # ε del segmento de cada SKU (fallback: global)
    if eps_sku is not None:
        est = est.join(eps_sku[["segmento", "eps", "se"]])
    else:
        est["segmento"] = "global"
        est["eps"], est["se"] = np.nan, np.nan
    est["eps"] = est.eps.fillna(eps_g)
    est["se"] = est.se.fillna(se_g)
    est["segmento"] = est.segmento.astype(str).replace("nan", "global")
    # stock actual y MESES DE STOCK (regla del negocio; ver meses_stock()).
    # "disponible" = stock en almacenes de VENTA, sin apartada. backorder =
    # REPOSICIÓN en camino (informativo; no es stock ni señal de demanda).
    if exist is not None:
        ex_ult = exist[exist.semana == exist.semana.max()].set_index("codigo")
        est["existencia"] = ex_ult.existencia.reindex(est.index)
        est["backorder"] = ex_ult.backorder.reindex(est.index)
        col_v = ("disp_venta" if "disp_venta" in ex_ult.columns else
                 ("disponible" if "disponible" in ex_ult.columns else "existencia"))
        est["disponible"] = ex_ult[col_v].reindex(est.index)
        est["meses_inv"], est["meses_fuente"] = meses_stock(pan, exist, est)
    else:
        est["existencia"], est["backorder"], est["disponible"] = np.nan, np.nan, np.nan
        est["meses_inv"], est["meses_fuente"] = np.nan, ""
    # señales de revisión (evidencia propia del SKU, confirmada con stock)
    # REMATE Y CLASIFICACIÓN del ERP (usuario 2026-07-31, semántica confirmada;
    # SIEMPRE la clasificación VIGENTE — cambia en el tiempo):
    #  · MI/MC = muestras ingeniería/cumplimiento, NO SE PUEDEN VENDER ⇒ fuera
    #    de todo análisis (el mismo modelo regresa cuando lo reclasifiquen)
    #  · remate activo (remate='S' o clasif R0-R4) ⇒ el producto ya no se
    #    comercializa: dejar agotar el stock — bloquea SUBIR y BAJAR (regla
    #    aplicada tras recomendar())
    ruta_cl = os.path.join(DATA, "reporte61", "proveedores_inventario.parquet")
    if not os.path.exists(ruta_cl):
        print("  AVISO: sin censo de inventario (proveedores_inventario.parquet) — "
              "remate/clasificación ERP NO aplicados; corre "
              "`extract_api.py proveedores`", flush=True)
    if os.path.exists(ruta_cl):
        _cl = pd.read_parquet(ruta_cl).set_index("codigo")
        # frescura del censo (ronda 3): la clasificación cambia en el tiempo
        if "fecha_censo" in _cl.columns:
            edad_c = (pd.Timestamp.today().normalize()
                      - pd.Timestamp(_cl.fecha_censo.iloc[0])).days
            if edad_c > 7:
                print(f"  AVISO: censo de inventario con {edad_c} días — la "
                      f"clasificación ERP puede estar desactualizada; corre "
                      f"`extract_api.py proveedores`", flush=True)
        else:
            print("  AVISO: censo de inventario sin fecha (formato viejo) — "
                  "regenerarlo con `extract_api.py proveedores`", flush=True)
        if "clasificacion" in _cl.columns:
            est["clasif_erp"] = _cl.clasificacion.reindex(est.index).fillna("")
            est["remate_erp"] = (_cl.remate.reindex(est.index).fillna("N").eq("S")
                                 | est.clasif_erp.isin(["R0", "R1", "R2", "R3", "R4"]))
            invendibles = est.clasif_erp.isin(["MI", "MC"])
            if invendibles.any():
                print(f"  clasificación ERP: {int(invendibles.sum())} MI/MC excluidos "
                      f"(muestras, no se venden) | remate activo: "
                      f"{int(est.remate_erp.sum()):,}", flush=True)
                est = est[~invendibles]
    if "remate_erp" not in est.columns:
        est["remate_erp"], est["clasif_erp"] = False, ""
    cayo, no_rota, precio_pre = señales_revision(pan, est, exist)
    est["cayo_tras_aumento"] = cayo
    est["no_rota"] = no_rota
    est["precio_pre_aumento"] = precio_pre
    # dinámica de demanda: crecimiento vs mercado y cobertura de stock
    est = est.join(metricas_dinamica(pan, est))
    # rol de precio (KVI / Sales Driver / Profit Gen / Estándar)
    est["rol"] = rol_precio(est)
    print(f"  roles de precio: {est.rol.value_counts().to_dict()}", flush=True)
    # proveedor (censo extract.py proveedores; atributo de reporte, no de modelo)
    ruta_prov = os.path.join(DATA, "reporte61", "proveedores.parquet")
    if os.path.exists(ruta_prov):
        prov = pd.read_parquet(ruta_prov).set_index("codigo").proveedor
        est["proveedor"] = prov.reindex(est.index).fillna("(sin dato)")
    else:
        est["proveedor"] = "(pendiente censo)"
    n_sobre = int((est.meses_inv >= 12).fillna(False).sum())
    print(f"SKUs evaluables: {len(est):,} | ε por segmento "
          f"{est.groupby('segmento').eps.first().round(2).to_dict()} | "
          f"u0 = {metodo} | señales: {int(cayo.sum())} cayó-tras-aumento, "
          f"{int(no_rota.sum())} no-rota, {n_sobre} sobrestock≥12m"
          f"{' | SIN datos de stock' if exist is None else ''}", flush=True)
    recos, esc = recomendar(est, bandas)
    # 🪜 TOPE ACUMULADO ±10pts (usuario 2026-08-04, defaults aprobados):
    # un modelo puede subir máx +10pts ACUMULADOS en ventana móvil de 12 meses,
    # medidos contra su precio ANCLA = el precio ANTES del primer cambio
    # APLICADO del motor en la ventana (registro monitoreo_cambios.csv — los
    # cambios aplicados llevan etiqueta del motor en el campo comentario del
    # ERP). RE-ANCLA POR COSTO: si el costo de reposición cambió ≥5% después
    # de ese cambio (y el costo del stock lo siguió — hubo compra), el
    # acumulado se reinicia desde el primer cambio posterior al movimiento de
    # costo. EXENTOS: frenos por reabasto (temporales, se revierten) y la
    # defensa de margen por costo (corre fuera del motor y puede romper tope).
    # Hasta que se acepten decisiones el registro está vacío ⇒ no topa a nadie.
    ruta_mon0 = os.path.join(os.path.dirname(DATA), "out", "monitoreo_cambios.csv")
    n_tope = 0
    if os.path.exists(ruta_mon0):
        mon0 = pd.read_csv(ruta_mon0)
        mon0 = mon0[(pd.to_datetime(mon0.fecha_aceptado)
                     >= pd.Timestamp.today().normalize() - pd.Timedelta(days=365))
                    & (mon0.cambio_pct > 0)].sort_values("fecha_aceptado")
        if len(mon0):
            # re-ancla por costo: semana del último salto ≥5% de costo_prov
            # confirmado por el costo del stock en mano (≥3% en igual sentido)
            resem = {}
            if exist is not None and "costo_prov" in exist.columns:
                cp = (exist.pivot_table(index="codigo", columns="semana",
                                        values="costo_prov", aggfunc="max")
                      .ffill(axis=1))
                vs = (exist.pivot_table(index="codigo", columns="semana",
                                        values="valor_stock", aggfunc="sum")
                      if "valor_stock" in exist.columns else None)
                et = (exist.pivot_table(index="codigo", columns="semana",
                                        values="existencia", aggfunc="sum")
                      if "existencia" in exist.columns else None)
                cs = (vs / et.replace(0, np.nan)).ffill(axis=1) \
                    if vs is not None and et is not None else None
                for cod in mon0.codigo.unique():
                    if cod not in cp.index:
                        continue
                    serie = cp.loc[cod].dropna()
                    salto = serie.pct_change().abs() >= 0.05
                    if salto.any():
                        f_salto = salto[salto].index.max()
                        compro = True
                        if cs is not None and cod in cs.index:
                            s2 = cs.loc[cod].dropna()
                            pre = s2[s2.index < f_salto]
                            post = s2[s2.index >= f_salto]
                            compro = (len(pre) and len(post) and pre.iloc[-1] > 0
                                      and abs(post.iloc[-1] / pre.iloc[-1] - 1) >= 0.03)
                        if compro:
                            resem[cod] = f_salto
            anclas_ac = {}
            for cod, d_ in mon0.groupby("codigo"):
                d_ = d_[pd.to_datetime(d_.fecha_aceptado)
                        >= pd.Timestamp(resem.get(cod, pd.Timestamp("2000-01-01")))]
                if len(d_):
                    anclas_ac[cod] = float(d_.precio_antes.iloc[0])
            if anclas_ac:
                anc_s = recos.codigo.map(anclas_ac)
                rev0 = recos.revisar.fillna("")
                tope_p = anc_s * 1.10
                m_t = ((recos.direccion == "SUBIR") & anc_s.notna()
                       & (recos.precio_sugerido > tope_p)
                       & ~rev0.str.contains("frenar"))
                # recorte al remanente; si el actual ya está en tope ⇒ MANTENER
                m_full = m_t & (recos.precio_actual >= tope_p * 0.999)
                m_clip = m_t & ~m_full
                recos.loc[m_clip, "precio_sugerido"] = tope_p[m_clip].round(2)
                recos.loc[m_clip, "cambio_pct"] = (100 * (tope_p[m_clip]
                                                   / recos.loc[m_clip, "precio_actual"] - 1)).round(1)
                recos.loc[m_clip, "revisar"] = ("tope acumulado +10pts (12m): paso "
                                                "recortado al remanente")
                recos.loc[m_full, "revisar"] = ("tope acumulado +10pts (12m) alcanzado: "
                                                "esperar ventana o re-ancla por costo")
                recos.loc[m_full, "precio_sugerido"] = recos.loc[m_full, "precio_actual"]
                recos.loc[m_full, "cambio_pct"] = 0.0
                recos.loc[m_full, "u_sem_proyectado"] = recos.loc[m_full, "u_sem_actual"]
                recos.loc[m_full, "utilidad_sem_sugerido"] = recos.loc[m_full, "utilidad_sem_mantener"]
                recos.loc[m_full, "direccion"] = "MANTENER"
                n_tope = int(m_t.sum())
    print(f"  🪜 tope acumulado +10pts/12m: {n_tope} modelos topados"
          + ("" if n_tope else " (registro de aplicados vacío o sin alcanzar tope"
             " — se activa al registrar cambios)"), flush=True)
    # 🌐 MEZCLA DE CANAL (usuario 2026-08-01, "Aplica las 4" punto 1 — evidencia
    # en docs/ANALISIS_CANAL_LINEA.md, 9,805 eventos pareados): el canal EN
    # LÍNEA acepta mejor las alzas chicas (retención 0.936 vs 0.868 en 2-5%).
    # Ajuste SOLO de confianza en SUBIR de ≤4pts (jamás dirección ni precio):
    #   pct_linea ≥ 0.70 y ≥10u/26sem ⇒ media→alta (el alza aterriza completa)
    #   pct_linea ≤ 0.30 ⇒ alta→media (ε efectiva del canal vendedor −3.04:
    #   la proyección es optimista y el vendedor puede amortiguar el alza)
    ruta_mz = os.path.join(DATA, "mezcla_canal.parquet")
    if os.path.exists(ruta_mz):
        mz = pd.read_parquet(ruta_mz).set_index("codigo")
        recos["pct_linea"] = recos.codigo.map(mz.pct_linea)
        u26 = recos.codigo.map(mz.u_26).fillna(0)
        m_sub = (recos.direccion == "SUBIR") & (recos.cambio_pct > 0) \
            & (recos.cambio_pct <= 4)
        m_up = m_sub & (recos.pct_linea >= 0.70) & (u26 >= 10) \
            & (recos.confianza == "media")
        m_dn = m_sub & (recos.pct_linea <= 0.30) & (u26 >= 10) \
            & (recos.confianza == "alta")
        recos.loc[m_up, "confianza"] = "alta"
        recos.loc[m_dn, "confianza"] = "media"
        # registrar SOLO cuando fue determinante (usuario 2026-08-01: sin chip
        # en el resumen; la explicación vive en el panel DETALLADO)
        recos["canal_ajuste"] = ""
        recos.loc[m_up, "canal_ajuste"] = "media→alta"
        recos.loc[m_dn, "canal_ajuste"] = "alta→media"
        print(f"  🌐 mezcla de canal: {int(m_up.sum()):,} SUBIR chicos "
              f"online-dominantes media→alta | {int(m_dn.sum()):,} "
              f"vendedor-dominantes alta→media", flush=True)
    else:
        recos["pct_linea"], recos["canal_ajuste"] = np.nan, ""
        print("  AVISO: sin mezcla_canal.parquet — ajuste de canal NO aplicado; "
              "corre `analisis_canal.py mezcla`", flush=True)
    # REMATE MANDA (usuario 2026-07-31): producto en remate ya no se
    # comercializa — si vende, dejar agotar el stock sin mover el precio.
    # Bloquea SUBIR (contradice liquidar) y BAJAR (el canal de remate gobierna).
    recos["remate"] = est.remate_erp.reindex(recos.codigo).fillna(False).values
    recos["clasif_erp"] = est.clasif_erp.reindex(recos.codigo).fillna("").values
    m_rem = recos.remate & recos.direccion.isin(["SUBIR", "BAJAR"])
    if m_rem.any():
        nivel = recos.loc[m_rem, "clasif_erp"].where(
            recos.loc[m_rem, "clasif_erp"].str.startswith("R"), "S")
        recos.loc[m_rem, "revisar"] = ("remate (" + nivel + "): dejar agotar stock — "
                                       "el precio lo gobierna el canal de remate")
        recos.loc[m_rem, "precio_sugerido"] = recos.loc[m_rem, "precio_actual"]
        recos.loc[m_rem, "cambio_pct"] = 0.0
        recos.loc[m_rem, "u_sem_proyectado"] = recos.loc[m_rem, "u_sem_actual"]
        recos.loc[m_rem, "utilidad_sem_sugerido"] = recos.loc[m_rem, "utilidad_sem_mantener"]
        recos.loc[m_rem, "direccion"] = "MANTENER"
        print(f"  remate manda: {int(m_rem.sum()):,} SUBIR/BAJAR convertidos a MANTENER "
              f"(dejar agotar; {int(recos.remate.sum()):,} SKUs en remate en el motor)",
              flush=True)
    # ⚓ ANCLA DE CANASTA (usuario 2026-07-31, "hagamos el cambio" — ver
    # docs/ANALISIS_VENTA_CRUZADA.md): subir un ancla deprime a sus compañeros
    # de folio (ε cruzada del FILTRO DURO, compañeros con lista quieta 12 sem:
    # attach 10-20%: −0.80 · 20-40%: −0.78 · >40%: −2.27; gradiente
    # dosis-respuesta verificado). Antes de emitir un SUBIR: margen arrastrado
    # = Σ_Y utilidad_Y × |ε_bucket| × Δprecio_X. Si arrastre ≥ 50% de la
    # ganancia propia ⇒ BLOQUEAR (proteger canasta). Frenos exentos (urgencia
    # de stock > canasta, como en KVI). Solo pares por bucket, jamás sueltos.
    ruta_anc = os.path.join(DATA, "anclas_canasta.parquet")
    if not os.path.exists(ruta_anc):
        print("  AVISO: sin mapa de anclas (anclas_canasta.parquet) — regla de "
              "canasta NO aplicada; corre `analisis_canasta.py anclas`", flush=True)
    if os.path.exists(ruta_anc):
        anc = pd.read_parquet(ruta_anc)
        # frescura del mapa (ronda 3): debe venir de la misma corrida del panel
        if "corte" in anc.columns:
            if str(pd.Timestamp(anc.corte.iloc[0]).date()) != str(pd.Timestamp(pan.semana.max()).date()):
                print(f"  AVISO: mapa de anclas con corte {pd.Timestamp(anc.corte.iloc[0]).date()} "
                      f"≠ panel {pd.Timestamp(pan.semana.max()).date()} — regenerar con "
                      f"`analisis_canasta.py anclas`", flush=True)
        util_map = recos.set_index("codigo").utilidad_sem_mantener
        # compañeros FUERA del motor (ronda 3): estimar su utilidad semanal del
        # panel (26 sem) en vez de contarlos como $0 — el fillna(0) dejaba el
        # 42.5% de los pares sin peso y el arrastre quedaba subestimado
        _u26 = pan[pan.semana >= pan.semana.max() - pd.Timedelta(weeks=26)]
        _util_pan = (_u26.assign(_ut=(_u26.neto_prom - _u26.costo_prom)
                                 * _u26.unidades_rec)
                     .groupby("codigo")._ut.sum() / 26)
        anc["util_Y"] = (anc.Y.map(util_map)
                         .fillna(anc.Y.map(_util_pan)).fillna(0.0).clip(lower=0))
        # fronteras alineadas con pd.cut del estudio (ronda 3): (0.2,0.4] es
        # bucket medio ⇒ attach exactamente 0.20/0.40 va al bucket INFERIOR
        anc["eps_x"] = np.where(anc.attach > 0.40, 2.27,
                                np.where(anc.attach > 0.20, 0.78, 0.80))
        arr = anc.assign(peso=anc.util_Y * anc.eps_x).groupby("X").agg(
            arrastre_unit=("peso", "sum"), n_comp=("Y", "nunique"))
        recos["ancla_n"] = recos.codigo.map(arr.n_comp).fillna(0).astype(int)
        # arrastre esperado del PASO propuesto de cada SKU
        recos["ancla_arrastre"] = (recos.codigo.map(arr.arrastre_unit).fillna(0.0)
                                   * recos.cambio_pct.abs() / 100).round(0)
        gan = recos.utilidad_sem_sugerido - recos.utilidad_sem_mantener
        rev_ = recos.revisar.fillna("")
        m_anc = ((recos.direccion == "SUBIR") & (recos.ancla_arrastre >= 0.5 * gan)
                 & (recos.ancla_arrastre > 0) & ~rev_.str.contains("frenar"))
        if m_anc.any():
            recos.loc[m_anc, "revisar"] = (
                "ancla de canasta: arrastre estimado −$"
                + recos.loc[m_anc, "ancla_arrastre"].map("{:,.0f}".format)
                + "/sem en " + recos.loc[m_anc, "ancla_n"].astype(str)
                + " compañeros ≥ 50% de la ganancia propia (+$"
                + gan[m_anc].map("{:,.0f}".format) + ") — proteger canasta")
            recos.loc[m_anc, "precio_sugerido"] = recos.loc[m_anc, "precio_actual"]
            recos.loc[m_anc, "cambio_pct"] = 0.0
            recos.loc[m_anc, "u_sem_proyectado"] = recos.loc[m_anc, "u_sem_actual"]
            recos.loc[m_anc, "utilidad_sem_sugerido"] = recos.loc[m_anc, "utilidad_sem_mantener"]
            recos.loc[m_anc, "direccion"] = "MANTENER"
            print(f"  ⚓ ancla de canasta: {int(m_anc.sum()):,} SUBIR bloqueados "
                  f"(arrastre estimado total −${recos.loc[m_anc, 'ancla_arrastre'].sum():,.0f}/sem "
                  f"que NO se destruirá)", flush=True)
    # PERIODO DE RE-TOQUE DINÁMICO (aprobado 2026-07-29): un SKU con medición
    # abierta (monitoreo) NO se re-toca hasta su fecha_retoque — calculada por
    # SKU según su tipo de venta y cantidad de datos (monitoreo.periodo_retoque:
    # 2-4 ciclos = 6-12 semanas). EXCEPCIONES que sí actúan (y censuran la
    # medición honestamente): freno, reversión de aumento dañino, sobrestock
    # manda; la defensa de margen por costo corre fuera del motor.
    ruta_mon = os.path.join(os.path.dirname(DATA), "out", "monitoreo_cambios.csv")
    if os.path.exists(ruta_mon):
        mon = pd.read_csv(ruta_mon)
        hoy = pd.Timestamp.today().normalize()
        abiertas = mon[mon.veredicto_12.isna() & mon.censurado_sem.isna()
                       & (pd.to_datetime(mon.fecha_retoque) > hoy)]
        if len(abiertas):
            f_ret = abiertas.sort_values("fecha_aceptado").groupby("codigo").fecha_retoque.last()
            rev_txt = recos.revisar.fillna("")
            excep = rev_txt.str.contains("frenar|revertir|sobrestock", regex=True)
            bloqueo = recos.codigo.isin(f_ret.index) & (recos.direccion != "MANTENER") & ~excep
            recos.loc[bloqueo, "precio_sugerido"] = recos.loc[bloqueo, "precio_actual"]
            recos.loc[bloqueo, "cambio_pct"] = 0.0
            recos.loc[bloqueo, "direccion"] = "MANTENER"
            recos.loc[bloqueo, "u_sem_proyectado"] = recos.loc[bloqueo, "u_sem_actual"]
            recos.loc[bloqueo, "utilidad_sem_sugerido"] = recos.loc[bloqueo, "utilidad_sem_mantener"]
            recos.loc[bloqueo, "revisar"] = ("en medición (periodo dinámico) — re-toque a partir de "
                                             + recos.loc[bloqueo, "codigo"].map(f_ret))
            print(f"  re-toque dinámico: {int(bloqueo.sum()):,} SKUs en medición "
                  f"protegidos (de {len(abiertas):,} mediciones abiertas)", flush=True)
    print("\n== RESUMEN DE RECOMENDACIONES ==", flush=True)
    print(recos.groupby(["confianza", "direccion"]).size().unstack(fill_value=0).to_string(),
          flush=True)
    n_rev = (recos.revisar != "").sum()
    print(f"  en revisión (SUBIR bloqueado por evidencia propia): {n_rev}", flush=True)
    dif = recos.utilidad_sem_sugerido - recos.utilidad_sem_mantener
    print(f"\nΔ utilidad semanal proyectada (solo confianza alta/media, puntual): "
          f"{dif[recos.confianza != 'baja'].sum():+,.0f} USD", flush=True)
    if recos[recos.direccion == 'BAJAR'].empty and (est.eps > -1).all():
        print("\nNOTA: ningún segmento salió elástico (|ε|<1 en todos) ⇒ el modelo aún "
              "no genera BAJAR por sí solo; las bajadas candidatas vienen de las señales "
              "de revisión. Con 24 meses se re-estima por segmento fino y EB por SKU.",
              flush=True)
    # SEGUNDA OPINIÓN (informativa; NO participa en el árbol de decisión):
    # forecast mensual del proceso AWS del negocio (aws_forecast.py).
    # aws_u_prox_mes = demanda pronosticada del próximo mes; aws_tendencia_pct =
    # cambio mediano m/m de los meses futuros; aws_wape_pct = error histórico
    # del propio forecast AWS en ese SKU (meses con fact+prediction).
    ruta_aws = os.path.join(DATA, "aws_forecast", "forecast_mensual.parquet")
    if os.path.exists(ruta_aws):
        aws = pd.read_parquet(ruta_aws)
        # regla del negocio (2026-07-27): solo meses que AÚN NO CONOCEMOS.
        # El mes en curso ya pasó (en su mayoría) ⇒ fuera; se usa del mes
        # siguiente en adelante ("agosto sí, julio no").
        mes_actual = pd.Timestamp.today().strftime("%Y-%m")
        fut = aws[(aws.tipo == "prediction") & (aws.mes > mes_actual)]
        if not fut.empty:
            piv = (fut.pivot_table(index="codigo", columns="mes", values="demanda",
                                   aggfunc="first").sort_index(axis=1))
            recos["aws_u_prox_mes"] = recos.codigo.map(piv.iloc[:, 0]).round(1)
            recos["aws_tendencia_pct"] = recos.codigo.map(
                100 * piv.pct_change(axis=1).median(axis=1)).round(1)
            u0_mes = recos.u_sem_actual * 4.345
            recos["aws_vs_u0_pct"] = np.where(
                u0_mes > 0, 100 * (recos.aws_u_prox_mes / u0_mes - 1), np.nan).round(0)
            ruta_w = os.path.join(DATA, "aws_forecast", "wape_por_sku.parquet")
            if os.path.exists(ruta_w):
                w = pd.read_parquet(ruta_w).set_index("codigo").wape_sku
                recos["aws_wape_pct"] = (100 * recos.codigo.map(w)).round(0)
            # amplitud territorial (aws_forecast.sucursales): demanda ancha vs
            # concentrada — misma lógica que "proyecto no es demanda"
            ruta_s = os.path.join(DATA, "aws_forecast", "sucursales.parquet")
            if os.path.exists(ruta_s):
                s = pd.read_parquet(ruta_s).set_index("codigo")
                recos["aws_suc_activas"] = recos.codigo.map(s.suc_activas)
                recos["aws_suc_alza_frac"] = recos.codigo.map(s.suc_alza_frac)
                recos["aws_suc_top_share"] = recos.codigo.map(s.suc_top_share)
                # MENCIÓN territorial en la explicación de SUBIR/BAJAR (usuario
                # 2026-07-27): el precio es NACIONAL — la geografía no decide,
                # pero contextualiza la acción general.
                dm = recos.direccion.isin(["SUBIR", "BAJAR"])
                ancha = dm & (recos.aws_suc_alza_frac >= 0.5) & (recos.aws_suc_activas >= 3)
                conc = dm & (recos.aws_suc_top_share >= 0.6) & (recos.aws_suc_activas >= 2)
                n_up = (recos.aws_suc_alza_frac * recos.aws_suc_activas).round()
                recos.loc[ancha, "explicacion"] = (
                    recos.loc[ancha, "explicacion"] + " · 🗺️ Demanda ANCHA: AWS prevé alza en "
                    + n_up[ancha].astype(int).astype(str) + " de "
                    + recos.loc[ancha, "aws_suc_activas"].astype(int).astype(str)
                    + " sucursales activas — el movimiento nacional tiene soporte territorial")
                recos.loc[conc, "explicacion"] = (
                    recos.loc[conc, "explicacion"] + " · 📍 Venta CONCENTRADA: "
                    + (100 * recos.loc[conc, "aws_suc_top_share"]).round(0).astype(int).astype(str)
                    + "% en una sola sucursal — puede ser cliente/proyecto local, no mercado general")
                print(f"  mención territorial: {int(ancha.sum())} ANCHA, "
                      f"{int(conc.sum())} CONCENTRADA en SUBIR/BAJAR", flush=True)
            print(f"  segunda opinión AWS: {recos.aws_u_prox_mes.notna().mean()*100:.0f}% "
                  f"de SKUs con forecast mensual ({piv.shape[1]} meses futuros)", flush=True)
    # SEÑALES WEB (informativas; NO deciden — usuario 2026-07-29): embudo de
    # syscom.mx vía API de BI (senales_web.py). La serie nació 2026-07-23; con
    # 3-4 semanas se evaluará si alguna señal entra a reglas (con aprobación).
    # web_conv_pct = compras web / vistas. Dos menciones sobre SUBIR/BAJAR:
    #  · 👁 MIRAN, NO COMPRAN: tráfico alto + conversión ≤ mitad de la mediana —
    #    demanda que NO se concretó (las ventas solas no la ven); matiza un SUBIR.
    #  · 🛒 CONVIERTE MUY BIEN: conversión ≥ 2× mediana con tráfico real —
    #    poder de precio observado en la vitrina; respalda un SUBIR.
    ruta_web = os.path.join(DATA, "senales_web.parquet")
    if os.path.exists(ruta_web):
        web = pd.read_parquet(ruta_web).set_index("codigo")
        recos["web_vistas"] = recos.codigo.map(web.vistas)
        recos["web_compras"] = recos.codigo.map(web.compras)
        recos["web_conv_pct"] = recos.codigo.map(web.conv_pct)
        dias_web = int(web.dias_ventana.iloc[0])
        recos["web_dias"] = np.where(recos.web_vistas.notna(), dias_web, np.nan)
        conv_med = web.loc[web.vistas >= 100, "conv_pct"].median()
        recos["web_conv_med"] = np.where(recos.web_vistas.notna(), conv_med, np.nan)
        dm = recos.direccion.isin(["SUBIR", "BAJAR"])
        traf = recos.web_vistas >= 100
        frios = dm & traf & (recos.web_conv_pct <= conv_med / 2)
        estrella = dm & traf & (recos.web_conv_pct >= 2 * conv_med)
        recos.loc[frios, "explicacion"] = (
            recos.loc[frios, "explicacion"] + " · 👁 Web: MIRAN, NO COMPRAN — "
            + recos.loc[frios, "web_vistas"].astype(int).map("{:,}".format)
            + " vistas en " + str(dias_web) + " días pero solo "
            + recos.loc[frios, "web_conv_pct"].map("{:.1f}".format)
            + "% termina en compra (la mitad del sitio o menos: mediana "
            + f"{conv_med:.0f}%) — hay demanda que no se concreta; señal informativa")
        recos.loc[estrella, "explicacion"] = (
            recos.loc[estrella, "explicacion"] + " · 🛒 Web: CONVIERTE MUY BIEN — "
            + recos.loc[estrella, "web_conv_pct"].map("{:.0f}".format)
            + "% de las vistas termina en compra (el doble del sitio o más: mediana "
            + f"{conv_med:.0f}%), con "
            + recos.loc[estrella, "web_vistas"].astype(int).map("{:,}".format)
            + " vistas en " + str(dias_web) + " días — señal informativa")
        print(f"  señales web ({dias_web} días): {recos.web_vistas.notna().mean()*100:.0f}% "
              f"de SKUs con actividad · conv mediana {conv_med:.1f}% · menciones: "
              f"{int(frios.sum())} MIRAN-NO-COMPRAN, {int(estrella.sum())} CONVIERTE-BIEN",
              flush=True)
    # T1.1 HOLDOUT EXPERIMENTAL (aprobado 2026-07-27, 15%): de los elegibles,
    # un 15% aleatorio NO se aplica este ciclo y sirve de CONTROL limpio (mismo
    # perfil, misma regla; la única diferencia es el sorteo). Estratificado por
    # dirección × tercil de volumen. EXENTOS: frenos por reabasto (urgencia de
    # inventario, coherente con F1) y reversiones de aumento dañino (dejar el
    # daño 3 semanas más cuesta). KVIs SÍ participan (es donde más vale una
    # elasticidad experimental propia). Semilla = fecha de corte: re-correr el
    # mismo ciclo reproduce el mismo sorteo.
    HOLDOUT_FRAC = 0.15
    rev = recos.revisar.fillna("")
    eleg = ((recos.direccion != "MANTENER") & (recos.confianza != "baja")
            & ~rev.str.contains("frenar") & ~rev.str.contains("revertir"))
    semilla = int(pd.Timestamp(pan.semana.max()).strftime("%Y%m%d"))
    rng = np.random.default_rng(semilla)
    recos["holdout"] = False
    sub = recos[eleg].copy()
    sub["_terc"] = pd.qcut(sub.u_sem_actual.rank(method="first"), 3, labels=False)
    for (_, _), g in sub.groupby(["direccion", "_terc"]):
        n = int(round(len(g) * HOLDOUT_FRAC))
        if n:
            recos.loc[rng.choice(g.index.values, size=n, replace=False), "holdout"] = True
    d_hold = (recos.utilidad_sem_sugerido - recos.utilidad_sem_mantener)[recos.holdout].sum()
    print(f"  holdout experimental 15%: {int(recos.holdout.sum()):,} de {int(eleg.sum()):,} "
          f"elegibles retenidos (uplift diferido ${3*d_hold:,.0f} una vez; semilla {semilla})",
          flush=True)
    # SELLO DE CORTE (auditoría 2026-07-30, C3): cada consumidor verifica que
    # recomendaciones y panel provengan del mismo corte antes de mezclarlos.
    recos["corte"] = pd.Timestamp(pan.semana.max()).date().isoformat()
    recos["generado_en"] = pd.Timestamp.now().isoformat(timespec="seconds")
    guardar_recos_local(recos, "recomendaciones")
    guardar_recos_local(esc, "escenarios")


if __name__ == "__main__":
    main()
