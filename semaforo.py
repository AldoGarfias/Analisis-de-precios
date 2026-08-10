# -*- coding: utf-8 -*-
"""SEMÁFORO COMPETITIVO (usuario 2026-08-10): clasifica cada par de
competencia con 4 firmas de evidencia — la práctica manda no reaccionar a un
precio ajeno sin contexto (la mayoría de los "más baratos" están atorados,
no vendiendo: medido 49 estancados vs 15 vendiendo de 99 amenazas).

Firmas por par:
  1. PERSISTENCIA del precio del competidor (días al nivel actual ±1%)
  2. TRAYECTORIA de SU stock (VENDIENDO baja ≥15% / REPONIENDO / ESTANCADO)
  3. CONSENSO (nº de competidores ≤ +5% del mejor precio)
  4. NUESTRO DAÑO (crecimiento de la venta del modelo, de recomendaciones)

Clasificación:
  AMENAZA REAL   gap<-10% Y persistente(≥7d) Y su stock rotando Y nuestra
                 venta cayendo (crecimiento < -5%/mes)
  REMATE AJENO   gap<-10% pero estancado/efímero ⇒ su inventario, no mercado
  ESPACIO        estamos ≥10% bajo el mejor competidor persistente
  NEUTRO         resto
Pares: EXACTOS con candados (marca coincide + |gap|≤60) tocan al motor;
EQUIVALENTES solo contexto con su confiabilidad (sim del vector).

Salida: data/competencia/semaforo.parquet (por par) +
data/competencia/semaforo_modelo.parquet (resumen por modelo SYSCOM para el
reporte). Corre a diario tras competencia.actualizar.
"""
import glob
import os
import re
import unicodedata

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.join(BASE, "data", "competencia")


# ALIAS de marca (rebrands/sub-líneas del MISMO fabricante — caso testigo
# 2026-08-10: TVC lista LBE5ACGEN2 como "UISP", nuestro catálogo "UBIQUITI")
ALIAS_MARCA = {
    "UISP": "UBIQUITI", "UBNT": "UBIQUITI", "UNIFI": "UBIQUITI",
    "HILOOK": "HIKVISION", "HILOOKBYHIKVISION": "HIKVISION",
    "UNIARCH": "UNV", "UNIARCHBYUNV": "UNV", "UNVUNIVIEW": "UNV",
    "UNIVIEW": "UNV", "IMOU": "DAHUA", "EZVIZ": "HIKVISION",
    "NETKEY": "PANDUIT", "WDWESTERNDIGITAL": "WESTERNDIGITAL",
    "WESTERNDIGITALWD": "WESTERNDIGITAL", "OMADA": "TPLINK", "MERCUSYS": "TPLINK",
    "TPLINK": "TPLINK", "CHAROFIL": "GEWISS",
}


def _nm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    if s in ("NULL", "NAN", "NONE"):
        return ""          # sin marca real ⇒ el candado de marca no aplica
    return ALIAS_MARCA.get(s, s)


def _firmas(db, fuente, modelo):
    """persistencia (días) y trayectoria de stock del competidor."""
    h = db[(db.fuente == fuente) & (db.modelo == modelo)].sort_values("fecha")
    if len(h) < 3:
        return np.nan, "sin dato"
    p_act, pers = h.precio_venta.iloc[-1], 0
    for p in h.precio_venta.iloc[::-1]:
        if p > 0 and abs(p / p_act - 1) <= 0.01:
            pers += 1
        else:
            break
    ex = h.existencia.dropna()
    tray = "sin dato"
    if len(ex) >= 6 and ex.iloc[:3].mean() > 0:
        d = ex.iloc[-3:].mean() / ex.iloc[:3].mean() - 1
        tray = "VENDIENDO" if d < -0.15 else ("REPONIENDO" if d > 0.15 else "ESTANCADO")
    return pers, tray


def correr():
    db = pd.concat([pd.read_parquet(f).assign(fuente=os.path.basename(f)[:-8])
                    for f in glob.glob(os.path.join(COMP, "db", "*.parquet"))],
                   ignore_index=True)
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    recos["neto0"] = np.where((recos.u_sem_actual > 0) & (recos.margen_actual > 0),
                              (recos.utilidad_sem_mantener / recos.u_sem_actual)
                              / recos.margen_actual, np.nan)
    R = recos.set_index("codigo")

    filas, descartes = [], []
    # ── EXACTOS con candados (marca coincide + |gap|≤60) ──
    o = pd.read_parquet(os.path.join(COMP, "syscom_vs_distribuidores.parquet"))
    ex = o[(o.via_final == "MODELO") & (o.nivel == "EXACTO")].copy()
    m_ult = (db.sort_values("fecha")
             .drop_duplicates(["fuente", "modelo"], keep="last")
             .set_index(["fuente", "modelo"]))
    for x in ex.itertuples():
        llave = (x.distribuidor, x.modelo_distribuidor)
        if llave not in m_ult.index or x.modelo_syscom not in R.index:
            continue
        c = m_ult.loc[llave]
        rr = R.loc[x.modelo_syscom]
        if not (c.precio_venta_usd > 0) or not (rr.neto0 > 0):
            continue
        # MARCA = solo CONFIRMACIÓN (usuario 2026-08-10: "el match 100% se
        # realiza por el MODELO; la marca solo confirma — si el modelo hace
        # match al 100% se toma por completo"). Discrepancia ⇒ bandera, no veto.
        mc, ms = _nm(c.marca), _nm(x.marca)
        marca_ok = not (len(mc) > 2 and len(ms) > 2
                        and mc not in ms and ms not in mc)
        if not marca_ok:
            descartes.append((x.distribuidor, x.modelo_distribuidor, mc, ms))
        gap = 100 * (c.precio_venta_usd / rr.neto0 - 1)
        if abs(gap) > 60:
            continue
        filas.append([x.modelo_syscom, "EXACTO", 1.0, x.distribuidor,
                      x.modelo_distribuidor, round(c.precio_venta_usd, 2),
                      round(float(rr.neto0), 2), round(gap, 1), marca_ok])
    # ── EQUIVALENTES (contexto, con su confiabilidad = sim del vector) ──
    ruta_eq = os.path.join(COMP, "equivalentes.parquet")
    if os.path.exists(ruta_eq):
        for x in pd.read_parquet(ruta_eq).itertuples():
            if x.nivel != "EQUIVALENTE" or x.codigo_syscom not in R.index:
                continue
            conf = float(getattr(x, "sim_vector", 0.99) or 0.99)
            filas.append([x.codigo_syscom, "EQUIVALENTE", round(conf, 2), x.fuente,
                          x.modelo_comp, x.precio_comp_usd, x.subtotal_syscom,
                          x.gap_pct, True])
    P = pd.DataFrame(filas, columns=["codigo", "match", "confiabilidad", "fuente",
                                     "modelo_comp", "precio_comp_usd",
                                     "subtotal_sys", "gap_pct", "marca_confirma"])
    # firmas por par
    pers, tray = [], []
    for x in P.itertuples():
        p_, t_ = _firmas(db, x.fuente, x.modelo_comp)
        pers.append(p_)
        tray.append(t_)
    P["persistencia_d"], P["stock_comp"] = pers, tray
    # consenso: nº de fuentes ≤ +5% del mejor precio del modelo
    mejor = P.groupby("codigo").precio_comp_usd.transform("min")
    P["consenso_n"] = P.assign(_c=P.precio_comp_usd <= mejor * 1.05) \
        .groupby("codigo")._c.transform("sum").astype(int)
    P["crecimiento"] = P.codigo.map(R.crecimiento if "crecimiento" in R.columns
                                    else pd.Series(dtype=float))
    # clasificación
    def clasifica(x):
        persistente = pd.notna(x.persistencia_d) and x.persistencia_d >= 7
        rotando = x.stock_comp in ("VENDIENDO", "REPONIENDO")
        dañado = pd.notna(x.crecimiento) and x.crecimiento < -0.05
        if x.gap_pct < -10 and persistente and rotando and dañado:
            return "AMENAZA REAL"
        if x.gap_pct < -10:
            return "REMATE AJENO"
        if x.gap_pct > 10 and persistente:
            return "ESPACIO"
        return "NEUTRO"
    P["semaforo"] = [clasifica(x) for x in P.itertuples()]
    P.to_parquet(os.path.join(COMP, "semaforo.parquet"), index=False)
    if descartes:
        D = pd.DataFrame(descartes, columns=["fuente", "modelo", "marca_comp", "marca_sys"])
        D.to_csv(os.path.join(BASE, "out", "competencia_marcas_descartadas.csv"), index=False)
        top = D.groupby(["marca_comp", "marca_sys"]).size().nlargest(5)
        print(f"  marca discrepante (ACEPTADOS con bandera): {len(D)} (posibles alias: "
              f"{top.to_dict()})", flush=True)

    # resumen POR MODELO para el reporte (el mejor par EXACTO manda; si solo
    # hay equivalentes, la confiabilidad del similar)
    res = []
    for cod, g in P.groupby("codigo"):
        gx = g[g.match == "EXACTO"]
        base = gx if len(gx) else g
        mejor_ = base.loc[base.precio_comp_usd.idxmin()]
        orden = {"AMENAZA REAL": 0, "REMATE AJENO": 1, "ESPACIO": 2, "NEUTRO": 3}
        sem = min(base.semaforo, key=lambda s: orden[s])
        res.append([cod, "EXACTO" if len(gx) else "EQUIVALENTE",
                    round(float(base.confiabilidad.max()), 2),
                    mejor_.fuente, round(float(mejor_.precio_comp_usd), 2),
                    round(float(mejor_.gap_pct), 1), int(mejor_.consenso_n), sem])
    M = pd.DataFrame(res, columns=["codigo", "match", "confiabilidad", "fuente",
                                   "mejor_comp_usd", "gap_pct", "consenso_n",
                                   "semaforo"])
    M.to_parquet(os.path.join(COMP, "semaforo_modelo.parquet"), index=False)
    print(f"semáforo: {len(P):,} pares ({(P.match=='EXACTO').sum():,} exactos) | "
          f"{len(M):,} modelos | {P.semaforo.value_counts().to_dict()}", flush=True)
    return P, M


if __name__ == "__main__":
    correr()
