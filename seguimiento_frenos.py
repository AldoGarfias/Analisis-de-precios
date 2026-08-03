# -*- coding: utf-8 -*-
"""Seguimiento DIARIO de los frenos por reabasto.

Un "freno" (subir precio para desacelerar la venta con stock corto) es temporal
por diseño: cuando llega la reposición hay que RE-DECIDIR — mantener el precio
nuevo (la escasez reveló poder de precio), revertirlo (frenó de más), o incluso
bajarlo (la reposición llegó tan grande que ahora hay sobrestock).

Modos:
  ./.venv/bin/python seguimiento_frenos.py registrar
      Da de alta en el registro los SKUs con señal "frenar" de la corrida
      vigente de escenarios (los que aún no estén en seguimiento).
      NOTA fase de pruebas: registra el precio SUGERIDO; cuando el piloto
      aplique cambios reales, el alta debe hacerse al aplicar.

  ./.venv/bin/python seguimiento_frenos.py            (modo diario, default)
      Para cada SKU en seguimiento consulta stock disponible (almacenes de
      venta), reposición y venta reciente:
        - intenta la BD en vivo (requiere VPN); si no hay conexión usa el
          último snapshot local y lo marca.
      Cuando la cobertura se recupera (≥ UMBRAL_OK semanas) el SKU pasa a
      REABASTECIDO con una recomendación:
        BAJAR      si con la reposición quedó en sobrestock (≥12 meses)
        REVERTIR   si la venta cayó >20% vs la base pre-freno
        MANTENER   si la demanda sostiene el precio nuevo

Registro persistente: out/seguimiento_frenos.csv
Cron instalado (días hábiles 8:30, con VPN):
  30 8 * * 1-5 cd /Users/aldogarfias/Downloads/handoff && ./.venv/bin/python seguimiento_frenos.py >> out/seguimiento_frenos.log 2>&1
"""
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
REG = os.path.join(BASE, "out", "seguimiento_frenos.csv")

UMBRAL_OK = 6      # semanas de cobertura para considerar "reabastecido"
CAIDA_REVERTIR = 0.20  # caída de venta vs base pre-freno que sugiere revertir


_COLS_TXT = ["codigo", "fecha_alta", "estado", "fecha_revision", "recomendacion"]


def _cargar_registro():
    if os.path.exists(REG):
        reg = pd.read_csv(REG)
    else:
        reg = pd.DataFrame(columns=[
            "codigo", "fecha_alta", "precio_antes", "precio_freno", "u_sem_base",
            "cobertura_alta", "reposicion_alta", "estado", "fecha_revision",
            "cobertura_hoy", "disponible_hoy", "venta_sem_hoy", "recomendacion",
            "alertado"])
    # columnas de texto siempre como object (una columna vacía llega float64 y
    # rechaza fechas/strings)
    for c in _COLS_TXT:
        if c in reg.columns:
            reg[c] = reg[c].fillna("").astype("object")
    if "alertado" not in reg.columns:
        reg["alertado"] = False
    reg["alertado"] = reg["alertado"].fillna(False).astype(bool)
    if "tipo" not in reg.columns:
        reg["tipo"] = "freno"
    reg["tipo"] = reg["tipo"].fillna("freno").astype("object")
    return reg


def registrar_dormidos():
    """Alta de dormidos SIN STOCK con reposición en camino (2ª capa): el precio
    no se actúa sin stock — al llegar la reposición se alerta para re-evaluar
    con el escenario de ese momento (invariante stock-first del negocio)."""
    ruta = os.path.join(BASE, "out", "segunda_capa_dormidos.csv")
    if not os.path.exists(ruta):
        print("no existe segunda_capa_dormidos.csv (corre dormidos.py)", flush=True)
        return
    dd = pd.read_csv(ruta)
    esp = dd[dd.direccion == "ESPERAR STOCK"]
    reg = _cargar_registro()
    nuevos = esp[~esp.codigo.isin(reg.codigo)]
    if nuevos.empty:
        print("sin dormidos nuevos que registrar", flush=True)
        return
    alta = pd.DataFrame({
        "codigo": nuevos.codigo,
        "tipo": "dormido",
        "fecha_alta": date.today().isoformat(),
        "precio_antes": nuevos.precio_epoca_viva,   # referencia: su época viva
        "precio_freno": nuevos.precio_hoy,          # lista vigente hoy
        "u_sem_base": nuevos.u_sem_epoca_viva,
        "cobertura_alta": 0.0,
        "reposicion_alta": nuevos.reposicion,
        "estado": "esperando_reposicion",
        "fecha_revision": "", "cobertura_hoy": np.nan, "disponible_hoy": np.nan,
        "venta_sem_hoy": np.nan, "recomendacion": "", "alertado": False,
    })
    reg = pd.concat([reg, alta], ignore_index=True)
    os.makedirs(os.path.dirname(REG), exist_ok=True)
    reg.to_csv(REG, index=False)
    print(f"registrados {len(alta)} dormidos esperando stock (total {len(reg)})", flush=True)


def registrar():
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    # candado (auditoría 2026-07-27): solo frenos APLICADOS (SUBIR); una señal
    # de freno abstención (confianza baja) no entra al seguimiento
    fren = recos[recos.revisar.fillna("").str.contains("frenar")
                 & (recos.direccion == "SUBIR")]
    reg = _cargar_registro()
    nuevos = fren[~fren.codigo.isin(reg.codigo)]
    if nuevos.empty:
        print("sin SKUs nuevos que registrar", flush=True)
        return
    alta = pd.DataFrame({
        "codigo": nuevos.codigo,
        "tipo": "freno",
        "fecha_alta": date.today().isoformat(),
        "precio_antes": nuevos.precio_actual,
        "precio_freno": nuevos.precio_sugerido,
        "u_sem_base": nuevos.u_sem_actual,
        "cobertura_alta": nuevos.cobertura_sem,
        "reposicion_alta": nuevos.reposicion,
        "estado": "esperando_reposicion",
        "fecha_revision": "", "cobertura_hoy": np.nan, "disponible_hoy": np.nan,
        "venta_sem_hoy": np.nan, "recomendacion": "", "alertado": False,
    })
    reg = pd.concat([reg, alta], ignore_index=True)
    os.makedirs(os.path.dirname(REG), exist_ok=True)
    reg.to_csv(REG, index=False)
    print(f"registrados {len(alta)} SKUs en seguimiento (total {len(reg)})", flush=True)


def _datos_vivos(codigos):
    """Stock disponible (venta), reposición y venta de últimos 14 días desde la
    BD. Devuelve (df, 'vivo') o (None, error) si no hay conexión/VPN."""
    try:
        from db import query
        in_list = ",".join("'" + c.replace("'", "''") + "'" for c in codigos)
        _, alm = query("SELECT id_almacen FROM `2015epcom`.`cat_almacen` WHERE vendible=1")
        ids = ",".join(str(int(r[0])) for r in alm)
        ayer = (date.today() - timedelta(days=1)).isoformat()
        _, stk = query(
            f"SELECT /*+ MAX_EXECUTION_TIME(60000) */ codigo, "
            f"SUM(CASE WHEN almacen IN ({ids}) THEN existencia ELSE 0 END), "
            f"MAX(cantidad_bo) FROM `reportes`.`valor_inventario` "
            f"WHERE fecha=%s AND codigo IN ({in_list}) GROUP BY codigo", (ayer,))
        hace14 = (date.today() - timedelta(days=14)).isoformat()
        _, vta = query(
            f"SELECT /*+ MAX_EXECUTION_TIME(60000) */ codigo, SUM(cantidad) "
            f"FROM `reportes`.`reporte_61` WHERE fecha >= %s AND estatus='Activa' "
            f"AND precio > 0 AND cantidad > 0 AND tipo_precio IN (1,3) "
            f"AND concepto NOT LIKE '%%royecto%%' AND codigo IN ({in_list}) "
            f"GROUP BY codigo", (hace14,))
        df = pd.DataFrame(stk, columns=["codigo", "disponible", "reposicion"]).set_index("codigo")
        v = pd.DataFrame(vta, columns=["codigo", "u14"]).set_index("codigo")
        df["venta_sem"] = (v.u14 / 2.0).reindex(df.index).fillna(0.0)
        for c in ["disponible", "reposicion", "venta_sem"]:
            df[c] = df[c].astype(float)
        return df, "vivo (BD)"
    except Exception as e:
        return None, f"sin conexión ({str(e)[:60]})"


def _datos_snapshot(codigos):
    """Fallback sin VPN: último snapshot semanal local."""
    ex = pd.read_parquet(os.path.join(BASE, "data", "reporte61", "existencias_sem.parquet"))
    ult = ex[ex.semana == ex.semana.max()].set_index("codigo")
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
    if "unidades_rec" in pan.columns:
        pan["unidades"] = pan["unidades_rec"].astype(float)
    v = (pan.sort_values("semana").groupby("codigo").tail(2)
         .groupby("codigo").unidades.mean())
    col = "disp_venta" if "disp_venta" in ult.columns else "disponible"
    df = pd.DataFrame({"disponible": ult[col].reindex(codigos),
                       "reposicion": ult["backorder"].reindex(codigos),
                       "venta_sem": v.reindex(codigos).fillna(0.0)})
    fecha = pd.Timestamp(ex.semana.max()).date()
    return df, f"snapshot local del {fecha} (sin VPN: datos no son de hoy)"


def revisar():
    reg = _cargar_registro()
    activos = reg[reg.estado.isin(["esperando_reposicion", "reabastecido"])]
    if activos.empty:
        print("no hay SKUs en seguimiento activo (corre `registrar` primero)", flush=True)
        return
    codigos = activos.codigo.tolist()
    datos, fuente = _datos_vivos(codigos)
    if datos is None:
        print(f"BD no disponible → fallback: ", end="", flush=True)
        datos, fuente = _datos_snapshot(codigos)
    print(f"fuente de datos: {fuente} | SKUs en seguimiento: {len(codigos)}", flush=True)

    en_vivo = fuente.startswith("vivo")
    hoy = date.today().isoformat()
    resumen = {"esperando": 0, "reabastecido": 0}
    nuevas_alertas = []
    for i, fila in reg.iterrows():
        if fila.estado not in ("esperando_reposicion", "reabastecido"):
            continue
        cod = fila.codigo
        if cod not in datos.index:
            continue
        d = datos.loc[cod]
        venta = max(float(d.venta_sem), 0.0)
        cobertura = float(d.disponible) / max(venta, 1e-9)
        reg.at[i, "fecha_revision"] = hoy
        reg.at[i, "cobertura_hoy"] = round(cobertura, 1)
        reg.at[i, "disponible_hoy"] = float(d.disponible)
        reg.at[i, "venta_sem_hoy"] = round(venta, 1)
        # DORMIDOS esperando stock: el disparador es LLEGÓ STOCK (venta≈0, la
        # cobertura no aplica); al llegar se re-evalúa con el escenario actual
        if str(getattr(fila, "tipo", "freno")) == "dormido":
            if float(d.disponible) > 0:
                rec = (f"YA TIENE STOCK ({d.disponible:,.0f} uds): re-evaluar el precio con "
                       f"el escenario ACTUAL (época viva {fila.precio_antes} vs lista hoy "
                       f"{fila.precio_freno}) — correr dormidos.py / motor")
                if en_vivo:
                    reg.at[i, "estado"] = "reabastecido"
                    reg.at[i, "recomendacion"] = rec
                    if not bool(fila.alertado):
                        reg.at[i, "alertado"] = True
                        nuevas_alertas.append((cod, cobertura, rec))
                resumen["reabastecido"] += 1
            else:
                resumen["esperando"] += 1
            continue
        if cobertura >= UMBRAL_OK:
            meses = float(d.disponible) / max(venta * 4.345, 1e-9)
            caida = 1 - venta / max(float(fila.u_sem_base), 1e-9)
            if meses >= 12:
                rec = f"BAJAR: la reposición dejó sobrestock ({meses:.0f} meses)"
            elif caida > CAIDA_REVERTIR:
                rec = (f"REVERTIR a {fila.precio_antes}: la venta cayó "
                       f"{100*caida:.0f}% vs base pre-freno")
            else:
                rec = "MANTENER el precio nuevo: la demanda lo sostiene"
            # ALERTAR solo con datos vivos (un snapshot viejo daría falsas
            # alarmas) y solo la PRIMERA vez que el SKU se reabastece
            if en_vivo:
                reg.at[i, "estado"] = "reabastecido"
                reg.at[i, "recomendacion"] = rec
                if not bool(fila.alertado):
                    reg.at[i, "alertado"] = True
                    nuevas_alertas.append((cod, cobertura, rec))
            resumen["reabastecido"] += 1
        else:
            resumen["esperando"] += 1

    reg.to_csv(REG, index=False)
    # log silencioso (para el cron); la ALERTA solo si hay reabastecidos nuevos
    print(f"[{hoy}] frenos: {resumen['esperando']} esperando, "
          f"{resumen['reabastecido']} reabastecidos, "
          f"{len(nuevas_alertas)} alertas nuevas ({fuente})", flush=True)
    if nuevas_alertas:
        ruta_alerta = os.path.join(BASE, "out", f"ALERTA_frenos_{hoy}.txt")
        with open(ruta_alerta, "w", encoding="utf-8") as f:
            f.write(f"ALERTA seguimiento de frenos — {hoy}\n")
            f.write("SKUs reabastecidos: hay que RE-DECIDIR el precio del freno\n\n")
            for cod, cob, rec in nuevas_alertas:
                f.write(f"  {cod:<20} cobertura {cob:.1f} sem → {rec}\n")
        print(f"  ALERTA escrita: {ruta_alerta}", flush=True)
        try:  # notificación nativa de macOS
            import subprocess
            msg = f"{len(nuevas_alertas)} SKU(s) reabastecidos: re-decidir precio del freno"
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg}" with title '
                            f'"Motor de Precios — Frenos" sound name "Glass"'],
                           timeout=10)
        except Exception:
            pass
    elif not en_vivo:
        print("  (sin VPN: no se emiten alertas con datos viejos)", flush=True)


PISO_MARGEN = 0.03      # mismo piso que escenarios.py
UMBRAL_COSTO = 0.02     # cambio de costo_prov que dispara la revisión
REG_COSTOS = None  # se define abajo


def revisar_costos():
    """PASS-THROUGH DE COSTO POR EVENTO (T2 aprobado 2026-07-27, 'lo aprobamos
    así'): vigilancia DIARIA del costo de reposición (costo_prov). Si el costo
    de un SKU subió ≥2% desde la emisión del ciclo Y con ese costo nuevo el
    precio actual ya no sostiene el piso de margen (+3pts sobre neto), se
    alerta con la LISTA MÍNIMA DEFENSIVA. Solo DEFIENDE el piso — no
    re-optimiza ni rompe la cadencia de 3 semanas (excepción acordada, análoga
    a F1). Alerta una sola vez por nivel de costo (registro anti-duplicados).
    """
    hoy = date.today().isoformat()
    reg_ruta = os.path.join(BASE, "out", "defensa_margen.csv")
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    r = recos[(recos.u_sem_actual > 0) & (recos.margen_actual > 0)].copy()
    # neto y costo unitario implícitos (misma derivación auditada del simulador)
    marg_unit = r.utilidad_sem_mantener / r.u_sem_actual
    r["neto0"] = marg_unit / r.margen_actual
    r["costo0"] = r.neto0 - marg_unit
    r["rho"] = r.neto0 / r.precio_actual

    # costo_prov BASE = snapshot de la semana de EMISIÓN del ciclo vigente
    # (auditoría 2026-07-31, N1: con ex.semana.max() la base se renovaba cada
    # lunes y una subida GRADUAL de +1.5%/sem jamás disparaba la defensa)
    ex = pd.read_parquet(os.path.join(BASE, "data", "reporte61", "existencias_sem.parquet"))
    if "costo_prov" not in ex.columns:
        print(f"[{hoy}] defensa de margen: snapshot sin costo_prov — omitido", flush=True)
        return
    sem_base = ex.semana.max()
    try:
        import glob as _g
        ciclos = sorted(_g.glob(os.path.join(BASE, "out", "ciclos", "ciclo_*.csv")))
        abiertos = [c for c in ciclos
                    if (pd.read_csv(c, nrows=1).estado == "abierto").all()]
        if abiertos:
            emision = pd.Timestamp(pd.read_csv(abiertos[0], nrows=1).fecha_emision.iloc[0])
            sems = pd.to_datetime(pd.Series(sorted(ex.semana.unique())))
            sem_base = sems.iloc[(sems - emision).abs().argmin()]
    except Exception as e:
        # sin ciclo legible la base cae al snapshot más reciente — avisar,
        # porque esa base "rodante" es justo lo que N1 corrigió (ronda 3, B5)
        print(f"[{hoy}] defensa de margen: no pude anclar la base a la emisión "
              f"del ciclo ({str(e)[:60]}) — uso el snapshot más reciente", flush=True)
    base_cp = ex[ex.semana == sem_base].set_index("codigo").costo_prov

    # costo_prov HOY (BD viva; sin VPN no se alerta con datos viejos)
    try:
        from db import query
        ayer = (date.today() - timedelta(days=1)).isoformat()
        _, rows = query(
            "SELECT /*+ MAX_EXECUTION_TIME(120000) */ codigo, MAX(costo_prov) "
            "FROM `reportes`.`valor_inventario` WHERE fecha=%s AND costo_prov>0 "
            "GROUP BY codigo", (ayer,))
        cp_hoy = pd.Series({c: float(v) for c, v in rows if v is not None})
    except Exception as e:
        print(f"[{hoy}] defensa de margen: sin BD viva ({str(e)[:50]}) — "
              f"sin alertas hoy", flush=True)
        return

    r["cp_base"] = r.codigo.map(base_cp)
    r["cp_hoy"] = r.codigo.map(cp_hoy)
    r = r.dropna(subset=["cp_base", "cp_hoy"])
    r = r[(r.cp_base > 0) & (r.cp_hoy / r.cp_base - 1 >= UMBRAL_COSTO)]
    # margen al precio ACTUAL si el costo de venta escala como el de reposición
    r["costo_def"] = r.costo0 * (r.cp_hoy / r.cp_base)
    r["margen_def"] = (r.neto0 - r.costo_def) / r.neto0
    r = r[r.margen_def < PISO_MARGEN]
    r["lista_min"] = (r.costo_def / (1 - PISO_MARGEN)) / r.rho

    reg = (pd.read_csv(reg_ruta) if os.path.exists(reg_ruta)
           else pd.DataFrame(columns=["codigo", "cp_alertado"]))
    ya = reg.set_index("codigo").cp_alertado if len(reg) else pd.Series(dtype=float)
    nuevas = r[r.codigo.map(ya).fillna(0) < r.cp_hoy * 0.999]
    print(f"[{hoy}] defensa de margen: {len(r)} SKUs bajo piso por costo nuevo, "
          f"{len(nuevas)} alertas nuevas", flush=True)
    if len(nuevas):
        ruta_a = os.path.join(BASE, "out", f"ALERTA_costos_{hoy}.txt")
        with open(ruta_a, "w", encoding="utf-8") as f:
            f.write(f"DEFENSA DE MARGEN por cambio de costo — {hoy}\n")
            f.write("El costo de reposición subió y el precio actual ya no sostiene "
                    "el piso (+3pts).\nAplicar la lista mínima defensiva SIN esperar "
                    "el ciclo (solo defensa, no re-optimización).\n\n")
            for _, x in nuevas.iterrows():
                f.write(f"  {x.codigo:<20} costo {x.cp_base:,.2f}→{x.cp_hoy:,.2f} "
                        f"({100*(x.cp_hoy/x.cp_base-1):+.1f}%) | margen quedaría "
                        f"{100*x.margen_def:.1f}% | lista {x.precio_actual:,.2f} → "
                        f"mín {x.lista_min:,.2f}\n")
        print(f"  ALERTA escrita: {ruta_a}", flush=True)
        try:
            import subprocess
            subprocess.run(["osascript", "-e",
                            f'display notification "{len(nuevas)} SKU(s) bajo piso de '
                            f'margen por costo nuevo" with title '
                            f'"Motor de Precios — Defensa de margen" sound name "Glass"'],
                           timeout=10)
        except Exception:
            pass
        act = pd.concat([reg[~reg.codigo.isin(nuevas.codigo)],
                         nuevas[["codigo", "cp_hoy"]].rename(columns={"cp_hoy": "cp_alertado"})])
        act.to_csv(reg_ruta, index=False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "registrar":
        registrar()
    elif len(sys.argv) > 1 and sys.argv[1] == "registrar-dormidos":
        registrar_dormidos()
    elif len(sys.argv) > 1 and sys.argv[1] == "costos":
        revisar_costos()
    else:
        # cada paso con su red (auditoría 2026-07-30, C7): un tropiezo en un
        # paso NO debe matar al resto de la cadena; los fallos se acumulan y
        # se reportan al final para que no pasen en silencio.
        fallos = []
        try:
            revisar()
        except (Exception, SystemExit) as e:
            fallos.append(f"frenos: {str(e)[:80]}")
            print(f"frenos: FALLÓ — {str(e)[:80]}", flush=True)
        try:
            revisar_costos()
        except (Exception, SystemExit) as e:
            fallos.append(f"costos: {str(e)[:80]}")
            print(f"costos: FALLÓ — {str(e)[:80]}", flush=True)
        # VIGÍA DIARIA — ANTES de monitoreo (auditoría 2026-07-31, N3 del
        # flujo: la censura de monitoreo compara contra la lista administrada
        # del snapshot de vigía; debe ser el de HOY, no el de ayer)
        try:
            import vigia_diaria
            vigia_diaria.correr()
        except (Exception, SystemExit) as e:
            fallos.append(f"vigía diaria: {str(e)[:80]}")
            print(f"vigía diaria: {str(e)[:80]}", flush=True)
        # monitoreo de decisiones ACEPTADAS (checkpoints de 7 días + veredicto
        # a 4 semanas) — mismo cron diario
        try:
            import monitoreo
            monitoreo.revisar()
        except (Exception, SystemExit) as e:
            fallos.append(f"monitoreo: {str(e)[:80]}")
            print(f"monitoreo: {str(e)[:60]}", flush=True)
        # vigilante de la API de BI: avisa cuando habiliten tablas nuevas
        try:
            import api_bi
            api_bi.vigilar()
        except (Exception, SystemExit) as e:
            fallos.append(f"vigilante BI: {str(e)[:80]}")
            print(f"vigilante BI: {str(e)[:60]}", flush=True)
        # CHECKPOINT SEMANAL del ciclo (usuario 2026-07-30): cache de venta
        # real vs banda proyectada + banderas — solo lunes
        if date.today().weekday() == 0:
            try:
                import checkpoint_semanal
                checkpoint_semanal.correr()
            except (Exception, SystemExit) as e:
                fallos.append(f"checkpoint: {str(e)[:80]}")
                print(f"checkpoint semanal: {str(e)[:80]}", flush=True)
            # CENSO DE INVENTARIO semanal (ronda 3, A1: el censo que trae
            # remate/clasificación no tenía dueño en el cron y envejecía en
            # silencio; escenarios avisa si pasa de 7 días)
            try:
                import extract_api
                extract_api.proveedores_inventario()
            except (Exception, SystemExit) as e:
                fallos.append(f"censo inventario: {str(e)[:80]}")
                print(f"censo inventario: {str(e)[:80]}", flush=True)
            # GOBERNANZA semanal (usuario 2026-08-01, punto 3 del estudio de
            # canal): fuga por descuento amortiguador tras alzas → dirección
            # comercial; y refresco de la mezcla de canal para monitoreo
            try:
                import analisis_canal
                analisis_canal.mezcla()
                analisis_canal.fuga()
            except (Exception, SystemExit) as e:
                fallos.append(f"canal/fuga: {str(e)[:80]}")
                print(f"canal/fuga: {str(e)[:80]}", flush=True)
        # EXAMEN MENSUAL DE FORECASTS (usuario 2026-07-31): los primeros días
        # de cada mes, calificar el mes cerrado (AWS / motor ex-ante / ingenuo
        # / ingenuo estacional / propio-ensamble) y archivar la predicción nueva.
        # Idempotente: examen salta si ya calificó; generar si ya archivó.
        if date.today().day <= 10:
            # try separados (ronda 3, R3-7): si el examen tropieza, el archivo
            # ex-ante del mes nuevo NO debe perderse — son independientes
            try:
                import forecast_mensual
                forecast_mensual.examen()
            except (Exception, SystemExit) as e:
                fallos.append(f"examen forecasts: {str(e)[:80]}")
                print(f"examen forecasts: {str(e)[:80]}", flush=True)
            try:
                import forecast_mensual
                forecast_mensual.generar()
            except (Exception, SystemExit) as e:
                fallos.append(f"archivo forecast mes: {str(e)[:80]}")
                print(f"archivo forecast mes: {str(e)[:80]}", flush=True)
            # el export de AWS SE CARGA LA 1ª SEMANA DEL MES (usuario
            # 2026-07-31): si a partir del día 3 aún no se integra el archivo
            # del mes corriente, recordarlo — sin él, la 2ª opinión AWS del
            # reporte y su voz en el examen trabajan con el export del mes pasado
            try:
                import glob as _g
                mes_act = date.today().strftime("%Y%m")
                if date.today().day >= 3 and not _g.glob(os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "data",
                        "aws_forecast", "archivo", f"pred_{mes_act}.parquet")):
                    print(f"⚠ falta integrar el export AWS de {mes_act} (llega la "
                          f"1ª semana): correr aws_forecast.py con el CSV nuevo",
                          flush=True)
                    fallos.append(f"falta export AWS {mes_act} (aws_forecast.py)")
                    import subprocess as _sp
                    _sp.run(["osascript", "-e",
                             'display notification "Falta integrar el export AWS del mes '
                             '(llega la 1ª semana): aws_forecast.py con el CSV nuevo" '
                             'with title "Motor de Precio v3"'], check=False, timeout=10)
            except Exception:
                pass
        if fallos:
            try:
                import subprocess as _sp
                _sp.run(["osascript", "-e",
                         f'display notification "Cron del motor: {len(fallos)} paso(s) '
                         f'fallaron — {"; ".join(fallos)[:120]}" '
                         f'with title "Motor de Precio v3 — REVISAR"'],
                        check=False, timeout=10)
            except Exception:
                pass
            print(f"\n⚠ RESUMEN: {len(fallos)} paso(s) del cron fallaron: {fallos}", flush=True)
