# -*- coding: utf-8 -*-
"""¿EL CANAL EN LÍNEA ACEPTA MEJOR LOS AUMENTOS? (hipótesis del usuario,
2026-08-01): "las ventas en línea aceptan más los aumentos de precio que las
ventas a través del vendedor — alguien sensibilizado o con objetivo de vender;
esto también se ve en folios con descuento_uno > 20 sin concepto de proyecto:
el vendedor otorgó un descuento adicional cuando un precio tuvo un aumento".

DISEÑO FIJADO ANTES DE VER RESULTADOS (filosofía campeón-retador):

  Canales (por `concepto` del renglón; el folio de pedido 576 NO viaja en
  reporte_61 — solo folios de factura):
    EN LÍNEA  = concepto contiene "línea/linea" (ventas en línea + V. Linea
                Sinosure ± Dlls) sin "proyecto"
    VENDEDOR  = todo lo demás EXCEPTO garantías ("garant") y proyectos
                ("royecto") — regla del usuario
  Higiene estándar del panel: tipo_precio ∈ {1,3}, precio>0, cantidad>0,
  kit != 'Si'.

  H1 (aceptación): sobre los MISMOS eventos de subida de lista ≥2% aislados
  del replay (analisis_eps_sku.eventos_precio), retención por canal =
  (unidades 4 sem post / unidades 8 sem pre) ajustada por el mercado DE SU
  PROPIO CANAL (el canal online crece secularmente — sin este ajuste la
  hipótesis se "confirma" sola). PAREADO: solo eventos con actividad previa
  en AMBOS canales (≥10 u y ≥3 semanas con venta por canal en las 8 pre).
  JUEZ: mediana de la diferencia pareada (ret_linea − ret_vendedor) y test de
  signos; ε implícita por canal = mediana ln(ret)/ln(1+ΔP).

  H2 (amortiguación): en esos mismos eventos, share de renglones con
  descuento_uno > 20% (sin proyecto) pre vs post POR CANAL, y pass-through
  al neto: Δ ln(neto unitario) / Δ ln(lista). Si el vendedor amortigua,
  su share d1>20 sube tras el aumento y su pass-through queda < línea.

Salida: data/analisis_canal.parquet (evento × canal) + impresión del estudio.
Estudio informativo: NO cambia reglas del motor (propuesta aparte si confirma).
"""
import glob
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

PRE_W, POST_W = 8, 4
MIN_U_PRE, MIN_SEM_PRE = 10, 3


def _lineas():
    """Renglones limpios con canal, d1 numérico y neto de línea."""
    partes = []
    rutas = sorted(glob.glob(os.path.join(DATA, "reporte61", "ventas_*.parquet")))
    if not rutas:
        raise SystemExit("analisis_canal: no hay ventas_*.parquet — corre la extracción")
    cols = ["fecha", "codigo", "cantidad", "precio", "tipo_precio", "concepto",
            "descuento_uno", "subtotal", "kit"]
    import pyarrow.parquet as pq
    for r in rutas:
        disp = set(pq.read_schema(r).names)
        v = pd.read_parquet(r, columns=[c for c in cols if c in disp])
        if "kit" in v.columns:
            v = v[v.kit != "Si"]
        for c in ("cantidad", "precio", "subtotal"):
            v[c] = pd.to_numeric(v[c], errors="coerce")
        v["tipo_precio"] = pd.to_numeric(v.tipo_precio, errors="coerce")
        v = v[(v.precio > 0) & (v.cantidad > 0) & v.tipo_precio.isin([1, 3])]
        con = v.concepto.astype(str).str.lower()
        es_proy = con.str.contains("royecto", na=False)
        es_gar = con.str.contains("garant", na=False)
        # l[íi?]nea: la ruta API devuelve "l?nea" (í rota) — JAMÁS filtrar por
        # caracteres acentuados (regla de migración; corregido 2026-08-01)
        es_linea = con.str.contains(r"l[íi?]nea", na=False, regex=True) & ~es_proy
        v["canal"] = np.where(es_linea, "linea",
                              np.where(es_proy | es_gar, "fuera", "vendedor"))
        v = v[v.canal != "fuera"]
        v["d1"] = pd.to_numeric(v.descuento_uno.astype(str).str.rstrip("%"),
                                errors="coerce")
        f = pd.to_datetime(v.fecha)
        if getattr(f.dt, "tz", None) is not None:
            f = f.dt.tz_localize(None)
        v["semana"] = f.dt.to_period("W-SUN").dt.start_time
        partes.append(v[["codigo", "semana", "canal", "cantidad", "subtotal", "d1"]])
    return pd.concat(partes, ignore_index=True)


def correr():
    ln = _lineas()
    print(f"renglones limpios: {len(ln):,} | canal: "
          f"{ln.canal.value_counts().to_dict()}", flush=True)

    g = ln.groupby(["codigo", "semana", "canal"]).agg(
        u=("cantidad", "sum"), neto=("subtotal", "sum"),
        n_lin=("d1", "size"), n_d1x=("d1", lambda s: int((s > 20).sum())),
    ).reset_index()

    # mercado semanal POR CANAL (ajuste de tendencia secular del canal)
    mkt = g.groupby(["semana", "canal"]).u.sum().unstack("canal")

    semanas = np.sort(g.semana.unique())
    sem_idx = {pd.Timestamp(s): k for k, s in enumerate(semanas)}
    piv = {c: {
        "u": g[g.canal == c].pivot(index="codigo", columns="semana", values="u")
             .reindex(columns=semanas),
        "neto": g[g.canal == c].pivot(index="codigo", columns="semana", values="neto")
                .reindex(columns=semanas),
        "nl": g[g.canal == c].pivot(index="codigo", columns="semana", values="n_lin")
              .reindex(columns=semanas),
        "nx": g[g.canal == c].pivot(index="codigo", columns="semana", values="n_d1x")
              .reindex(columns=semanas),
    } for c in ("linea", "vendedor")}

    import analisis_eps_sku as aes
    ev = aes.eventos_precio()
    subidas = ev[ev.rel >= 0.02]
    print(f"eventos de subida aislados: {len(subidas):,}", flush=True)

    filas = []
    for _, e in subidas.iterrows():
        t0 = sem_idx.get(pd.Timestamp(e.semana))
        if t0 is None or t0 < PRE_W or t0 > len(semanas) - POST_W - 1:
            continue
        fila = {"codigo": e.codigo, "semana": str(e.semana)[:10],
                "rel": round(float(e.rel), 4)}
        ok = True
        for c in ("linea", "vendedor"):
            P = piv[c]
            if e.codigo not in P["u"].index:
                ok = False
                break
            u = P["u"].loc[e.codigo].fillna(0.0).values
            pre, post = u[t0 - PRE_W:t0], u[t0:t0 + POST_W]
            if pre.sum() < MIN_U_PRE or (pre > 0).sum() < MIN_SEM_PRE:
                ok = False
                break
            m = mkt[c].reindex(semanas).values
            aj = (np.nanmean(m[t0:t0 + POST_W]) / np.nanmean(m[t0 - PRE_W:t0]))
            ret = (post.mean() / pre.mean()) / aj if aj > 0 else np.nan
            nt = P["neto"].loc[e.codigo].values
            nu_pre = np.nansum(nt[t0 - PRE_W:t0]) / max(pre.sum(), 1e-9)
            u_post = post.sum()
            nu_post = (np.nansum(nt[t0:t0 + POST_W]) / u_post if u_post > 0 else np.nan)
            nl = P["nl"].loc[e.codigo].fillna(0.0).values
            nx = P["nx"].loc[e.codigo].fillna(0.0).values
            sh_pre = nx[t0 - PRE_W:t0].sum() / max(nl[t0 - PRE_W:t0].sum(), 1e-9)
            sh_post = (nx[t0:t0 + POST_W].sum() / nl[t0:t0 + POST_W].sum()
                       if nl[t0:t0 + POST_W].sum() > 0 else np.nan)
            fila.update({f"ret_{c}": round(ret, 4),
                         f"dneto_{c}": (round(nu_post / nu_pre - 1, 4)
                                        if nu_pre > 0 and np.isfinite(nu_post) else np.nan),
                         f"sh_d1x_pre_{c}": round(sh_pre, 4),
                         f"sh_d1x_post_{c}": (round(sh_post, 4)
                                              if np.isfinite(sh_post) else np.nan)})
        if ok:
            filas.append(fila)

    C = pd.DataFrame(filas)
    C.to_parquet(os.path.join(DATA, "analisis_canal.parquet"), index=False)
    print(f"\n== EVENTOS PAREADOS (venta previa en AMBOS canales): {len(C):,} ==",
          flush=True)
    if C.empty:
        return C

    # ── H1: aceptación ──
    C["dif"] = C.ret_linea - C.ret_vendedor
    n_pos = int((C.dif > 0).sum())
    from scipy import stats
    p_sign = stats.binomtest(n_pos, len(C), 0.5).pvalue if len(C) else np.nan
    print(f"\nH1 — retención post-aumento (ajustada por su canal):", flush=True)
    print(f"  EN LÍNEA mediana: {C.ret_linea.median():.3f} | "
          f"VENDEDOR: {C.ret_vendedor.median():.3f} | "
          f"dif pareada mediana: {C.dif.median():+.3f} "
          f"(línea gana en {n_pos}/{len(C)}, p-signos={p_sign:.4f})", flush=True)
    lg = np.log(C.rel + 1)
    eps_l = (np.log(C.ret_linea.clip(0.01)) / lg).median()
    eps_v = (np.log(C.ret_vendedor.clip(0.01)) / lg).median()
    print(f"  ε implícita al AUMENTO: línea {eps_l:+.2f} vs vendedor {eps_v:+.2f}",
          flush=True)
    # dosis-respuesta (firma causal, como en canasta)
    C["mag"] = pd.cut(C.rel, [0.02, 0.05, 0.10, 1.0],
                      labels=["2-5%", "5-10%", ">10%"])
    for m, d in C.groupby("mag", observed=True):
        print(f"    {m:<6} n={len(d):>4} | dif mediana {d.dif.median():+.3f} | "
              f"línea {d.ret_linea.median():.3f} vs vend {d.ret_vendedor.median():.3f}",
              flush=True)

    # ── H2: amortiguación del vendedor ──
    print(f"\nH2 — ¿el vendedor amortigua con descuento extra (d1>20, sin proyecto)?",
          flush=True)
    for c in ("linea", "vendedor"):
        d_sh = (C[f"sh_d1x_post_{c}"] - C[f"sh_d1x_pre_{c}"]).dropna()
        print(f"  {c:<9} share d1>20: pre {C[f'sh_d1x_pre_{c}'].median():.3f} → "
              f"post {C[f'sh_d1x_post_{c}'].median():.3f} | Δ mediana por evento "
              f"{d_sh.median():+.4f} (sube en {(d_sh > 0).mean():.0%})", flush=True)
    print(f"  pass-through al NETO unitario (Δneto/Δlista, mediana):", flush=True)
    for c in ("linea", "vendedor"):
        pt = (C[f"dneto_{c}"] / C.rel).replace([np.inf, -np.inf], np.nan).dropna()
        print(f"    {c:<9} {pt.median():+.2f} (1.0 = traslada todo el aumento)",
              flush=True)
    return C


def mezcla():
    """MEZCLA DE CANAL por SKU (aprobada 2026-08-01, "Aplica las 4" punto 1):
    % de unidades EN LÍNEA en las últimas 26 semanas → data/mezcla_canal.parquet
    (codigo, pct_linea, u_26). escenarios.py la usa para ajustar CONFIANZA de
    los SUBIR chicos (2-4pts): online-dominante (≥70%) media→alta (el estudio
    mide retención 0.936 en alzas 2-5%); vendedor-dominante (≤30%) alta→media
    (la ε efectiva del canal vendedor −3.04 hace optimista la proyección).
    ADEMÁS guarda data/canal_semanal.parquet (canal vendedor: codigo, semana,
    n_lin, n_d1x) para la bandera de medición contaminada en monitoreo.py."""
    ln = _lineas()
    corte = ln.semana.max()
    r26 = ln[ln.semana >= corte - pd.Timedelta(weeks=26)]
    g = r26.groupby(["codigo", "canal"]).cantidad.sum().unstack("canal").fillna(0.0)
    mz = pd.DataFrame({
        "pct_linea": g.get("linea", 0.0) / (g.sum(axis=1)).clip(lower=1e-9),
        "u_26": g.sum(axis=1),
    }).reset_index()
    mz["pct_linea"] = mz.pct_linea.round(4)
    mz.to_parquet(os.path.join(DATA, "mezcla_canal.parquet"), index=False)
    # serie semanal del canal VENDEDOR (renglones y renglones con d1>20)
    ven = ln[ln.canal == "vendedor"]
    cs = ven.groupby(["codigo", "semana"]).agg(
        n_lin=("d1", "size"), n_d1x=("d1", lambda s: int((s > 20).sum()))
    ).reset_index()
    cs.to_parquet(os.path.join(DATA, "canal_semanal.parquet"), index=False)
    dom = (mz.pct_linea >= 0.70) & (mz.u_26 >= 10)
    vnd = (mz.pct_linea <= 0.30) & (mz.u_26 >= 10)
    print(f"mezcla de canal: {len(mz):,} SKUs (corte {pd.Timestamp(corte).date()}) | "
          f"online-dominantes (≥70%, ≥10u): {int(dom.sum()):,} | "
          f"vendedor-dominantes (≤30%): {int(vnd.sum()):,} | "
          f"serie vendedor: {len(cs):,} celdas", flush=True)
    return mz


def fuga():
    """REPORTE DE GOBERNANZA (aprobado 2026-08-01, punto 3): eventos de subida
    de lista recientes (12 semanas) donde el canal vendedor amortiguó con
    descuento extra (d1>20 sin proyecto) — cuánto margen del alza se regaló,
    por SKU. Palanca de dirección comercial (autoridad de descuento), el motor
    solo la ilumina. Salida: out/fuga_descuentos.csv + .html."""
    import analisis_eps_sku as aes
    ln = _lineas()
    ven = ln[ln.canal == "vendedor"].copy()
    ven["extra"] = np.where(
        ven.d1 > 20,
        ven.subtotal / (1 - ven.d1 / 100).clip(lower=0.01) * (ven.d1 - 20) / 100,
        0.0)
    g = ven.groupby(["codigo", "semana"]).agg(
        extra=("extra", "sum"), n_lin=("d1", "size"),
        n_d1x=("d1", lambda s: int((s > 20).sum()))).reset_index()
    ev = aes.eventos_precio()
    corte = g.semana.max()
    sub = ev[(ev.rel >= 0.02)
             & (pd.to_datetime(ev.semana) >= corte - pd.Timedelta(weeks=12))]
    filas = []
    for _, e in sub.iterrows():
        t0 = pd.Timestamp(e.semana)
        d = g[g.codigo == e.codigo]
        post = d[(d.semana >= t0) & (d.semana < t0 + pd.Timedelta(weeks=4))]
        pre = d[(d.semana >= t0 - pd.Timedelta(weeks=8)) & (d.semana < t0)]
        ex_post, ex_pre4 = post.extra.sum(), pre.extra.sum() / 2
        sh_post = post.n_d1x.sum() / max(post.n_lin.sum(), 1)
        sh_pre = pre.n_d1x.sum() / max(pre.n_lin.sum(), 1)
        if ex_post - ex_pre4 > 50 and sh_post - sh_pre > 0.05:
            filas.append({"codigo": e.codigo, "semana_alza": str(e.semana)[:10],
                          "alza_pct": round(100 * e.rel, 1),
                          "fuga_4sem_usd": round(ex_post - ex_pre4, 0),
                          "share_d1x_pre": round(sh_pre, 3),
                          "share_d1x_post": round(sh_post, 3),
                          "renglones_post": int(post.n_lin.sum())})
    F = pd.DataFrame(filas).sort_values("fuga_4sem_usd", ascending=False) \
        if filas else pd.DataFrame(columns=["codigo"])
    ruta = os.path.join(BASE, "out", "fuga_descuentos.csv")
    F.to_csv(ruta, index=False)
    html = ["<!doctype html><meta charset='utf-8'><title>Fuga por descuento "
            "amortiguador</title><style>body{font-family:system-ui;margin:24px}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;"
            "padding:4px 10px;text-align:right}th{background:#f4f4f4}"
            "td:first-child{text-align:left}</style>",
            f"<h2>Descuento amortiguador tras alzas de lista — últimas 12 semanas"
            f" (corte {pd.Timestamp(corte).date()})</h2>",
            "<p>Alzas de lista ≥2% donde el canal VENDEDOR aumentó los renglones "
            "con descuento_uno &gt; 20 (sin proyecto) y regaló parte del alza. "
            "El neto NO subió lo que la lista: gobernanza de descuento, no motor. "
            "Evidencia: docs/ANALISIS_CANAL_LINEA.md.</p>"]
    if len(F):
        html.append("<table><tr><th>SKU</th><th>Semana del alza</th><th>Alza</th>"
                    "<th>Fuga 4 sem (USD)</th><th>d1&gt;20 pre</th><th>post</th>"
                    "<th>Renglones</th></tr>")
        for _, x in F.head(200).iterrows():
            html.append(f"<tr><td>{x.codigo}</td><td>{x.semana_alza}</td>"
                        f"<td>+{x.alza_pct}%</td><td>${x.fuga_4sem_usd:,.0f}</td>"
                        f"<td>{100*x.share_d1x_pre:.0f}%</td>"
                        f"<td>{100*x.share_d1x_post:.0f}%</td>"
                        f"<td>{x.renglones_post}</td></tr>")
        html.append("</table>")
        html.append(f"<p><b>Total fuga estimada: "
                    f"${F.fuga_4sem_usd.sum():,.0f}</b> en {len(F)} eventos.</p>")
    else:
        html.append("<p>Sin eventos con fuga material en la ventana.</p>")
    ruta_h = os.path.join(BASE, "out", "fuga_descuentos.html")
    open(ruta_h, "w", encoding="utf-8").write("\n".join(html))
    print(f"fuga por amortiguador: {len(F)} eventos, "
          f"${F.fuga_4sem_usd.sum() if len(F) else 0:,.0f} → {ruta} / {ruta_h}",
          flush=True)
    return F


if __name__ == "__main__":
    import sys as _sys
    modo = _sys.argv[1] if len(_sys.argv) > 1 else ""
    {"mezcla": mezcla, "fuga": fuga}.get(modo, correr)()
