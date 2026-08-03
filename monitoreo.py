# -*- coding: utf-8 -*-
"""MONITOREO DE DECISIONES APLICADAS — con TRES HORIZONTES (aprobado 2026-07-28).

Cada decisión aceptada se mide en periodos de 7 días y recibe VEREDICTO en tres
horizontes (utilidad realizada vs contrafactual de mantener, ajuste de mercado):
  · 4 semanas  — impacto inmediato (preliminar)
  · 8 semanas  — adaptación y sustitución
  · 12 semanas — efecto comercial completo (definitivo)

CENSURA (la condición del diseño): si el SKU recibe OTRO cambio de precio a
mitad de la medición, los horizontes que aún no cerraban quedan marcados
CENSURADO — jamás se finge una medición limpia sobre un experimento pisado.
Detección: (a) nueva aceptación del mismo código en el registro; (b) la lista
vigente del panel difiere ≥1% del precio aplicado.

Comandos:
  aceptar CODIGO... | aceptar --ciclo   registra y CONGELA la proyección
  revisar                               (cron diario) checkpoints + veredictos
  diagnostico                           índice de éxito del motor por horizonte

Registro: out/monitoreo_cambios.csv · Diagnóstico: out/diagnostico_motor.csv
"""
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
REG = os.path.join(BASE, "out", "monitoreo_cambios.csv")
HORIZONTES = [4, 8, 12]
MAX_SEM = 12

def periodo_retoque(clase, u0, clientes):
    """PERIODO DE RE-TOQUE DINÁMICO por SKU (aprobado 2026-07-29).

    Primero se analiza cuántos datos hay y qué tipo de producto es (la
    clasificación que el motor ya hace), y de ahí sale cuántos CICLOS de
    3 semanas debe durar la medición antes de poder re-tocar el MISMO SKU:

      base por tipo de venta:  suave 2 · errática 3 · esporádica 3 · irregular 4
      ajuste por señal:  mucha señal (≥30 u/sem y ≥8 clientes/sem) → −1 ciclo
                         señal rala (<3 u/sem o <2 clientes/sem)   → +1 ciclo
      acotado a [2, 4] ciclos  →  re-toque a las 6 / 9 / 12 semanas

    Respaldo: test-and-learn pide 4-10 sem (4-6 en bajo tráfico); nuestra
    duración histórica mediana es 9 sem; y los horizontes de veredicto son
    4/8/12 — así la medición nunca se auto-censura. Excepciones que SÍ pueden
    actuar durante la medición (censuran honestamente): freno por reabasto,
    defensa de margen por costo, reversión de aumento dañino, sobrestock manda.
    """
    base = {"suave": 2, "errática": 3, "esporádica": 3, "intermitente": 3,
            "grumosa (lumpy)": 4}.get(str(clase), 3)
    if u0 >= 30 and clientes >= 8:
        base -= 1
    elif u0 < 3 or clientes < 2:
        base += 1
    return int(min(4, max(2, base)))


_COLS = (["codigo", "fecha_aceptado", "direccion", "cambio_pct", "precio_antes",
          "precio_nuevo", "u_base", "u_proyectado", "u_p10", "u_p90",
          "marg_unit_antes", "marg_unit_nuevo", "clase_serie", "rol", "holdout", "ciclos_medicion", "fecha_retoque"]
         + [f"sem{k}_real" for k in range(1, MAX_SEM + 1)]
         + ["censurado_sem", "causa_censura", "en_banda_4", "desc_amortiguador"]
         + [c for h in HORIZONTES for c in (f"veredicto_{h}", f"error_proy_{h}",
                                            f"mercado_{h}")])


_COLS_TXT = (["causa_censura", "en_banda_4", "desc_amortiguador"]
             + [f"veredicto_{h}" for h in [4, 8, 12]])


def _carga_reg():
    if os.path.exists(REG):
        r = pd.read_csv(REG)
        for c in _COLS:
            if c not in r.columns:
                r[c] = np.nan
    else:
        r = pd.DataFrame(columns=_COLS)
    for c in _COLS_TXT:      # columnas de texto: jamás float64 (LossySetitem)
        r[c] = r[c].astype(object)
    return r


def aceptar(codigos):
    """Registra decisiones ACEPTADAS/APLICADAS con su proyección congelada.
    Si el mismo código tenía una medición abierta, la CENSURA (nuevo cambio)."""
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    if codigos == ["--ciclo"]:
        # AUDITORÍA 2026-07-30 (C4): el ciclo se acepta desde el SNAPSHOT
        # CONGELADO al emitir (out/ciclos/ciclo_*.csv abierto), NO desde las
        # recos vivas — re-correr escenarios a mitad de ciclo puede cambiar el
        # sorteo del holdout y contaminaría el grupo de control del cierre.
        import glob as _glob
        rutas = sorted(_glob.glob(os.path.join(BASE, "out", "ciclos", "ciclo_*.csv")))
        abiertas_c = [r for r in rutas
                      if (pd.read_csv(r, nrows=1).estado == "abierto").all()]
        if not abiertas_c:
            raise SystemExit("no hay ciclo ABIERTO — emite con ciclo.py antes de aceptar")
        snap_c = pd.read_csv(abiertas_c[0])
        sel = snap_c[(snap_c.direccion != "MANTENER") & (snap_c.confianza != "baja")
                     & ~snap_c.holdout.fillna(False)]
        print(f"aceptando el ciclo completo desde el snapshot "
              f"{os.path.basename(abiertas_c[0])} (no-holdout): {len(sel):,} SKUs",
              flush=True)
    else:
        # sello de corte para la ruta manual (auditoría 2026-07-31, N9): las
        # recos vivas deben ser del corte del panel vigente antes de congelar
        if "corte" in recos.columns:
            pan_c = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"),
                                    columns=["semana"])
            c_pan = pd.Timestamp(pan_c.semana.max()).date().isoformat()
            if str(recos.corte.iloc[0]) != c_pan:
                raise SystemExit(f"SELLO DE CORTE: recos al {recos.corte.iloc[0]} pero "
                                 f"panel al {c_pan} — re-corre escenarios.py antes de aceptar")
        sel = recos[recos.codigo.isin(codigos)]
        faltan = set(codigos) - set(sel.codigo)
        if faltan:
            print(f"OJO sin recomendación (no registrados): {sorted(faltan)}", flush=True)
    if sel.empty:
        print("nada que registrar", flush=True)
        return
    reg = _carga_reg()
    hoy = date.today()
    # censurar mediciones abiertas del mismo código (llegó un cambio nuevo)
    abiertas = reg.veredicto_12.isna() & reg.censurado_sem.isna() & reg.codigo.isin(sel.codigo)
    for idx in reg[abiertas].index:
        sem_c = max(1, (pd.Timestamp(hoy) - pd.Timestamp(reg.at[idx, "fecha_aceptado"])).days // 7)
        reg.at[idx, "censurado_sem"] = sem_c
        reg.at[idx, "causa_censura"] = "nuevo cambio aplicado"
    if abiertas.any():
        print(f"  censuradas {int(abiertas.sum())} mediciones abiertas (nuevo cambio "
              f"en el mismo código)", flush=True)
    alta = pd.DataFrame({
        "codigo": sel.codigo, "fecha_aceptado": hoy.isoformat(),
        "direccion": sel.direccion, "cambio_pct": sel.cambio_pct,
        "precio_antes": sel.precio_actual, "precio_nuevo": sel.precio_sugerido,
        "u_base": sel.u_sem_actual, "u_proyectado": sel.u_sem_proyectado,
        "u_p10": sel.u_sem_p10, "u_p90": sel.u_sem_p90,
        "marg_unit_antes": sel.utilidad_sem_mantener / sel.u_sem_actual.clip(lower=1e-9),
        "marg_unit_nuevo": sel.utilidad_sem_sugerido / sel.u_sem_proyectado.clip(lower=1e-9),
        "clase_serie": sel.get("clase_serie", "sin clase"),
        "tipo_precio": sel.get("tipo_precio", np.nan),
        "rol": sel.rol, "holdout": sel.holdout.fillna(False),
        "ciclos_medicion": [periodo_retoque(c, u, n) for c, u, n in
                            zip(sel.get("clase_serie", "sin clase"),
                                sel.u_sem_actual, sel.get("n_clientes", sel.u_sem_actual*0))],
    })
    alta["fecha_retoque"] = [(hoy + timedelta(weeks=3*c)).isoformat()
                             for c in alta.ciclos_medicion]
    out = pd.concat([reg, alta], ignore_index=True)
    out.to_csv(REG, index=False)
    print(f"registrados {len(alta):,} cambios aceptados el {hoy} → {REG}", flush=True)


def _ventas_vivas(codigos, desde):
    """Venta recurrente DIARIA por SKU desde `desde` (BD viva). None sin VPN."""
    try:
        from db import query
        in_list = ",".join("'" + c.replace("'", "''") + "'" for c in codigos)
        _, rows = query(
            f"SELECT /*+ MAX_EXECUTION_TIME(120000) */ codigo, fecha, SUM(cantidad) "
            f"FROM `reportes`.`reporte_61` WHERE fecha >= %s AND estatus='Activa' "
            f"AND precio > 0 AND cantidad > 0 AND tipo_precio IN (1,3) "
            f"AND concepto NOT LIKE '%%royecto%%' AND codigo IN ({in_list}) "
            f"GROUP BY codigo, fecha", (desde,))
        df = pd.DataFrame(rows, columns=["codigo", "fecha", "u"])
        df["fecha"] = pd.to_datetime(df.fecha)
        df["u"] = df.u.astype(float)
        return df
    except Exception as e:
        print(f"  sin BD viva ({str(e)[:50]}) — checkpoints pospuestos", flush=True)
        return None


def _factor_mercado(fecha_ini, n_sem, pan):
    fin = pd.Timestamp(fecha_ini) + pd.Timedelta(weeks=n_sem)
    if pd.Timestamp(pan.semana.max()) < fin - pd.Timedelta(days=3):
        return np.nan
    tot = pan.groupby("semana")._u.sum()
    ini = pd.Timestamp(fecha_ini)
    ahora = tot[(tot.index >= ini) & (tot.index < fin)].mean()
    antes = tot[(tot.index >= ini - pd.Timedelta(weeks=4)) & (tot.index < ini)].mean()
    return float(ahora / antes) if antes and antes > 0 else np.nan


def revisar():
    """Checkpoints de 7 días, CENSURA por cambio de lista, y veredictos 4/8/12."""
    hoy = date.today()
    reg = _carga_reg()
    abiertos = reg[reg.veredicto_12.isna() & reg.censurado_sem.isna()]
    if abiertos.empty:
        print(f"[{hoy}] monitoreo: sin mediciones abiertas", flush=True)
        return
    pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
    pan["_u"] = pan.unidades_rec.astype(float)
    # CENSURA (b) contra la LISTA ADMINISTRADA fresca (vigía diaria), NO contra
    # el precio observado en ventas del panel (auditoría 2026-07-30, C1): el
    # panel es semanal y refleja el precio solo cuando hay ventas — comparar
    # contra él censuraba en falso toda aceptación ≥1% en días. Sin snapshot de
    # vigía NO se censura por precio (mejor no censurar que censurar en falso).
    import glob as _glob
    snaps = sorted(_glob.glob(os.path.join(BASE, "data", "vigia", "snap_*.parquet")))
    listas_adm = pd.read_parquet(snaps[-1]).set_index("codigo") if snaps else None
    if listas_adm is None:
        print("  (sin snapshot de vigía: censura por cambio de lista pospuesta)", flush=True)
    # serie del canal VENDEDOR para la bandera de descuento amortiguador
    # (analisis_canal.py mezcla; si no existe, la bandera simplemente no corre)
    ruta_cs = os.path.join(BASE, "data", "canal_semanal.parquet")
    cs_ven = None
    if os.path.exists(ruta_cs):
        cs_ven = pd.read_parquet(ruta_cs)
        cs_ven["semana"] = pd.to_datetime(cs_ven.semana)
        cs_ven = cs_ven.set_index("codigo")

    def _lista_adm(cod, tp, ref):
        if listas_adm is None or cod not in listas_adm.index:
            return np.nan
        l1 = float(listas_adm.at[cod, "precio_1"] or np.nan)
        l3 = float(listas_adm.at[cod, "precio_3"] or np.nan)
        if tp == 1:
            return l1
        if tp == 3:
            return l3
        # tipo desconocido (registros viejos): la lista más cercana al aplicado
        cands = [x for x in (l1, l3) if np.isfinite(x)]
        return min(cands, key=lambda x: abs(x - ref)) if cands else np.nan

    vivas = _ventas_vivas(abiertos.codigo.unique().tolist(), abiertos.fecha_aceptado.min())
    if vivas is None:
        return
    n_chk = n_ver = n_cen = 0
    for idx, x in abiertos.iterrows():
        ini = pd.Timestamp(x.fecha_aceptado)
        dias = (pd.Timestamp(hoy) - ini).days
        # CENSURA (b): la lista ADMINISTRADA vigente ya no es el precio aplicado
        tp = pd.to_numeric(getattr(x, "tipo_precio", np.nan), errors="coerce")
        l_hoy = _lista_adm(x.codigo, tp, x.precio_nuevo)
        if np.isfinite(l_hoy) and x.precio_nuevo > 0 and abs(l_hoy / x.precio_nuevo - 1) >= 0.01:
            reg.at[idx, "censurado_sem"] = max(1, dias // 7)
            reg.at[idx, "causa_censura"] = (f"lista administrada cambió a {l_hoy:,.2f} "
                                            f"(aplicado: {x.precio_nuevo:,.2f})")
            n_cen += 1
            continue
        v = vivas[vivas.codigo == x.codigo]
        for k in range(1, MAX_SEM + 1):
            col = f"sem{k}_real"
            if pd.notna(reg.at[idx, col]) or dias < 7 * k:
                continue
            a, b = ini + pd.Timedelta(days=7 * (k - 1)), ini + pd.Timedelta(days=7 * k)
            reg.at[idx, col] = round(float(v[(v.fecha >= a) & (v.fecha < b)].u.sum()), 1)
            n_chk += 1
        # veredictos por horizonte (solo si el horizonte cerró y no está censurado)
        for h in HORIZONTES:
            if pd.notna(reg.at[idx, f"veredicto_{h}"]) or dias < 7 * h:
                continue
            reales = [reg.at[idx, f"sem{k}_real"] for k in range(1, h + 1)]
            if any(pd.isna(r_) for r_ in reales):
                continue
            u_real = float(np.mean(reales))
            fac = _factor_mercado(x.fecha_aceptado, h, pan)
            fac_uso = fac if np.isfinite(fac) else 1.0
            util_real = u_real * x.marg_unit_nuevo
            util_cf = x.u_base * fac_uso * x.marg_unit_antes
            reg.at[idx, f"veredicto_{h}"] = ("ÉXITO" if util_real > util_cf * 1.02 else
                                             ("FRACASO" if util_real < util_cf * 0.98
                                              else "NEUTRO"))
            reg.at[idx, f"error_proy_{h}"] = round(u_real / max(x.u_proyectado, 1e-9), 3)
            reg.at[idx, f"mercado_{h}"] = round(fac_uso, 4) if np.isfinite(fac) else np.nan
            if h == 4:
                reg.at[idx, "en_banda_4"] = bool(x.u_p10 <= u_real <= x.u_p90)
                # 🚩 DESCUENTO AMORTIGUADOR (usuario 2026-08-01, punto 2 del
                # estudio de canal): en un SUBIR, si el share de renglones con
                # d1>20 del canal VENDEDOR salta >5pts tras el alza, el neto NO
                # subió lo que la lista — el veredicto no midió el precio nuevo.
                # Se marca (no se censura): la utilidad realizada sigue siendo
                # real, pero la ε que se aprenda de aquí está contaminada.
                if float(x.cambio_pct) > 0 and cs_ven is not None \
                        and x.codigo in cs_ven.index:
                    s = cs_ven.loc[[x.codigo]]
                    t0 = pd.Timestamp(x.fecha_aceptado)
                    post = s[(s.semana >= t0) & (s.semana < t0 + pd.Timedelta(weeks=4))]
                    pre = s[(s.semana >= t0 - pd.Timedelta(weeks=8)) & (s.semana < t0)]
                    if post.n_lin.sum() >= 10 and pre.n_lin.sum() >= 10:
                        sh_d = (post.n_d1x.sum() / post.n_lin.sum()
                                - pre.n_d1x.sum() / pre.n_lin.sum())
                        reg.at[idx, "desc_amortiguador"] = bool(sh_d > 0.05)
            n_ver += 1
    reg.to_csv(REG, index=False)
    n_amo = int((reg.desc_amortiguador == True).sum())  # noqa: E712 (col object)
    print(f"[{hoy}] monitoreo: {len(abiertos):,} abiertas | {n_chk} checkpoints | "
          f"{n_ver} veredictos | {n_cen} censuradas (lista cambió) | "
          f"{n_amo} con 🚩 descuento amortiguador (ε contaminada)", flush=True)


def diagnostico():
    """Índice de éxito del motor POR HORIZONTE (4 preliminar / 8 / 12 definitivo)."""
    reg = _carga_reg()
    print(f"== DIAGNÓSTICO DEL MOTOR ({len(reg):,} decisiones aceptadas) ==", flush=True)
    if reg.empty:
        print("  registro vacío: nada aceptado aún", flush=True)
        return
    cen = reg[reg.censurado_sem.notna()]
    print(f"  censuradas: {len(cen):,} (medición interrumpida por nuevo cambio — "
          f"honestidad, no pérdida)", flush=True)
    filas = []
    for h in HORIZONTES:
        c = reg[reg[f"veredicto_{h}"].notna()]
        if c.empty:
            print(f"\n  HORIZONTE {h} SEM: sin veredictos aún "
                  f"(el primero llega {h} semanas después de aceptar)", flush=True)
            continue
        ex = (c[f"veredicto_{h}"] == "ÉXITO").mean()
        err = c[f"error_proy_{h}"].dropna()
        print(f"\n  HORIZONTE {h} SEM ({'preliminar' if h == 4 else 'definitivo' if h == 12 else 'adaptación'}): "
              f"n={len(c):,} | ÉXITO {100*ex:.0f}% | error de proyección mediano "
              f"{err.median():.2f} (1.00 = perfecto)", flush=True)
        for d_, g in c.groupby("direccion"):
            print(f"    {d_:<8} n={len(g):<5} éxito "
                  f"{100*(g[f'veredicto_{h}']=='ÉXITO').mean():.0f}%", flush=True)
        filas.append({"horizonte": h, "n": len(c), "exito": round(float(ex), 3),
                      "error_mediano": round(float(err.median()), 3)})
    c4 = reg[reg.en_banda_4.notna()]
    if len(c4):
        print(f"\n  cobertura de banda (4 sem): {100*c4.en_banda_4.mean():.0f}% "
              f"(objetivo ~80%)", flush=True)
    if filas:
        pd.DataFrame(filas).to_csv(os.path.join(BASE, "out", "diagnostico_motor.csv"),
                                   index=False)
        print(f"\nguardado out/diagnostico_motor.csv", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "revisar"
    if cmd == "aceptar":
        if len(sys.argv) < 3:
            raise SystemExit("uso: monitoreo.py aceptar CODIGO... | --ciclo")
        aceptar(sys.argv[2:])
    elif cmd == "diagnostico":
        diagnostico()
    else:
        revisar()
