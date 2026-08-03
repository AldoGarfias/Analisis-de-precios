# -*- coding: utf-8 -*-
"""Construye el panel `codigo x semana` desde data/reporte61/*.parquet.

Primera versión (fase de pruebas): limpieza + descomposición de precio/descuentos
+ agregado semanal + sanity checks. El modelo econométrico va en modelo.py.

Precio y descuentos (esquema B2B, identidad verificado en 95% de renglones; el 5% restante trae un descuento EXTRA negociado (factor 0.90-0.99) que los 3 campos no capturan — sin impacto: el neto SIEMPRE se toma del subtotal real cobrado):
    neto_unit = precio × (1-descuento_uno) × (1-descuento) × (1-desc_fin/100)
    subtotal  = neto_unit × cantidad
  - `precio`        = precio de LISTA administrado (precio_1 si tipo_precio=1,
                      precio_3 si tipo_precio=3). Es la PALANCA de elasticidad.
  - `subtotal/cant` = precio realizado NETO (endógeno). Se usa para el MARGEN real
                      y el análisis de descuento. El margen es sobre el neto, NO
                      sobre la lista.
  - `costo_venta`   = costo TOTAL de la línea (unitario × cantidad, verificado:
                      constante por unidad dentro del mismo SKU). Unitario =
                      costo_venta / cantidad.
  - `descuento_uno` ≈ 20%; si ≠20 => negociación especial/proyecto (ver `concepto`).
  - `descuento`     = 2º descuento por cliente (según `clasificacion_descuento`).
  - `desc_fin`      = descuento por forma de pago en puntos % (4=contado ... 0).

Limpieza (lecciones REVISION_V2):
  - Excluir SKUs de servicio (ENVIO/VOUCHER/comisiones) — blocklist.
  - Excluir líneas centavo/promo (no son observaciones de precio reales).
  - Solo tipo_precio ∈ {1,3} (1=lista, 3=oferta). precio>0 ya excluye componentes
    de kit (su precio va en la línea del kit).
  - Assert de moneda (#6): precio/subtotal/costo en misma escala.

LIMITACIONES (solo reporte_61): sin existencias no se marcan stockouts (#2); panel
sobre semanas OBSERVADAS (con venta). El precio de lista es la serie administrada
`precio`, no un proxy.

Salida: data/panel.parquet
"""
import glob
import os

import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
IN_GLOB = os.path.join(DATA, "reporte61", "ventas_*.parquet")
OUT = os.path.join(DATA, "panel.parquet")

PREFIJOS_SERVICIO = ("ENVIO", "VOUCHER", "FLETE")
CODIGOS_SERVICIO = {
    "CGODIV", "comision_bancaria", "SUSTITUCION", "MISMODIA", "PRORELAY",
    "EPCOMGPSMENSUAL", "LAPGPS", "MO", "ENVIOGRATIS", "ENVIOV",
}

PISO_USD = 0.50
RATIO_PROMO = 0.10
MIN_SEMANAS = 8


def _es_servicio(cod):
    s = str(cod)
    if s in CODIGOS_SERVICIO:
        return True
    up = s.upper()
    return any(up.startswith(p) for p in PREFIJOS_SERVICIO)


def _pct(serie):
    """'20.0000%' -> 0.20 (tinytext de MySQL)."""
    return serie.astype(str).str.rstrip("%").replace({"None": np.nan, "nan": np.nan}).astype(float) / 100.0


def cargar():
    archivos = sorted(glob.glob(IN_GLOB))
    if not archivos:
        raise SystemExit(f"No hay parquets en {IN_GLOB}. Corre extract.py primero.")
    df = pd.concat([pd.read_parquet(a) for a in archivos], ignore_index=True)
    for c in ["precio", "costo_venta", "tipo_cambio", "subtotal", "desc_fin"]:
        df[c] = df[c].astype(float)
    df["fecha"] = pd.to_datetime(df["fecha"])
    df["cantidad"] = df["cantidad"].astype(int)
    df["d_uno"] = _pct(df["descuento_uno"])
    df["d_dos"] = _pct(df["descuento"])
    df["d_fin"] = df["desc_fin"].fillna(0) / 100.0
    # dedup de renglones exactos (~0.7%; duplicados literales de la vista)
    n0 = len(df)
    df = df.drop_duplicates(subset=["folio", "codigo", "fecha", "cantidad", "subtotal"])
    print(f"  -{n0-len(df):,} renglones duplicados exactos (folio,codigo,fecha,cant,subtotal)",
          flush=True)
    # costo UNITARIO (costo_venta viene por línea completa)
    df["costo_unit"] = df.costo_venta / df.cantidad
    print(f"crudo: {len(df):,} renglones | {df.codigo.nunique():,} códigos | "
          f"{df.fecha.min().date()} -> {df.fecha.max().date()}", flush=True)
    return df


def limpiar(df):
    n0 = len(df)
    serv = df.codigo.map(_es_servicio)
    df = df[~serv]
    print(f"  -{serv.sum():,} renglones de servicio", flush=True)
    # KITS: modelos virtuales que agrupan componentes (sin stock propio; su
    # precio agrupa varios artículos). Se descartan en esta etapa.
    ruta_kits = os.path.join(DATA, "reporte61", "kits.parquet")
    if os.path.exists(ruta_kits):
        kits = set(pd.read_parquet(ruta_kits).codigo)
        es_kit = df.codigo.isin(kits)
        df = df[~es_kit]
        print(f"  -{es_kit.sum():,} renglones de KIT ({len(kits)} códigos virtuales)", flush=True)
    else:
        print("  (sin kits.parquet: corre `extract.py kits` para excluir kits)", flush=True)
    promo = (df.precio < PISO_USD) | (df.precio < RATIO_PROMO * df.costo_unit)
    df = df[~promo]
    print(f"  -{promo.sum():,} renglones centavo/promo", flush=True)
    ok = (df.cantidad > 0) & (df.precio > 0) & (df.costo_venta > 0) & (df.subtotal > 0)
    df = df[ok]
    tp = df.tipo_precio.isin([1, 3])
    print(f"  -{(~tp).sum():,} renglones tipo_precio ∉ {{1,3}}", flush=True)
    df = df[tp]
    print(f"limpio: {len(df):,} renglones ({100*len(df)/n0:.0f}% del crudo) | "
          f"tipo_precio: {df.tipo_precio.value_counts().to_dict()}", flush=True)
    return df.copy()


def derivar(df):
    """Precio neto realizado y verificación de la identidad de descuentos."""
    df["neto_unit"] = df.subtotal / df.cantidad
    # identidad: precio*(1-d1)*(1-d2)*(1-dfin) debe ≈ neto_unit
    esperado = df.precio * (1 - df.d_uno.fillna(0)) * (1 - df.d_dos.fillna(0)) * (1 - df.d_fin)
    err = (esperado - df.neto_unit).abs() / df.neto_unit.clip(lower=1e-9)
    ok = (err < 0.02).mean()
    print(f"  identidad de descuentos verificada en {100*ok:.1f}% de renglones "
          f"(neto = precio×(1-d1)(1-d2)(1-dfin))", flush=True)
    df["especial"] = (df.d_uno.round(4) != 0.20)  # descuento_uno ≠ 20 => proyecto/negociación
    df["es_proyecto"] = df.concepto.str.contains("royecto", na=False)
    return df


def asserts_moneda(df):
    tc = df.tipo_cambio
    assert tc.between(10, 26).mean() > 0.95, f"tipo_cambio raro: {tc.describe()}"
    mrg = ((df.neto_unit - df.costo_unit) / df.neto_unit).median()
    assert -0.2 < mrg < 0.95, f"margen neto mediano implausible ({mrg:.2f}): revisa moneda"
    print(f"  assert moneda OK: tc {tc.min():.1f}-{tc.max():.1f} | margen NETO mediano {mrg:.2f}",
          flush=True)


def construir_panel(df):
    df["semana"] = df.fecha.dt.to_period("W").dt.start_time
    # excluir semanas de borde truncadas (no cubren lunes-domingo completos):
    # inflan/deflactan el agregado y corrompen tendencia y elasticidad.
    dias_sem = df.groupby("semana")["fecha"].nunique()
    completas = dias_sem[dias_sem >= 6].index          # >=6 días observados
    df = df[df.semana.isin(completas)]
    print(f"  semanas completas: {len(completas)} (excluidas {len(dias_sem)-len(completas)} de borde)",
          flush=True)

    # precio de lista SIN mezcla de regímenes: mediana del tipo MODAL del SKU-semana
    # (el promedio ponderado 1/3 mide mezcla de canal, no la palanca administrada).
    # Vectorizado: agg por (codigo,semana,tipo) y luego el tipo con más renglones
    # (empate → tipo menor, mismo criterio que Series.mode().iat[0]).
    por_tipo = (df.groupby(["codigo", "semana", "tipo_precio"])
                  .agg(n=("cantidad", "size"), p_med=("precio", "median"))
                  .reset_index()
                  .sort_values(["codigo", "semana", "n", "tipo_precio"],
                               ascending=[True, True, False, True]))
    modal = (por_tipo.drop_duplicates(["codigo", "semana"])
             [["codigo", "semana", "tipo_precio", "p_med"]]
             .rename(columns={"p_med": "precio_lista"}))

    # unidades RECURRENTES: sin ventas de proyecto (cuentan como venta, pero NO
    # como demanda recurrente — regla del negocio). Los indicadores de demanda
    # (u0, tendencia, elasticidad, meses de stock) usan esta serie.
    df["cant_rec"] = df.cantidad.where(~df.es_proyecto, 0)
    pan = df.groupby(["codigo", "semana"]).agg(
        unidades=("cantidad", "sum"),
        unidades_rec=("cant_rec", "sum"),
        subtotal_tot=("subtotal", "sum"),
        costo_tot=("costo_venta", "sum"),
        d_dos_prom=("d_dos", "mean"),
        dfin_prom=("d_fin", "mean"),
        pct_especial=("especial", "mean"),
        pct_proyecto=("es_proyecto", "mean"),
        tc=("tipo_cambio", "median"),
        n_obs=("cantidad", "size"),
        n_clientes=("codigo_cliente", "nunique"),
    ).reset_index()
    pan["neto_prom"] = pan.subtotal_tot / pan.unidades      # realizado neto
    pan["costo_prom"] = pan.costo_tot / pan.unidades        # unitario ponderado
    pan = pan.drop(columns=["subtotal_tot", "costo_tot"])
    pan = pan.merge(modal, on=["codigo", "semana"], how="left")
    pan["margen"] = (pan.neto_prom - pan.costo_prom) / pan.neto_prom
    sem_por_sku = pan.groupby("codigo")["semana"].transform("nunique")
    pan["activo"] = sem_por_sku >= MIN_SEMANAS
    return pan


def clasificar_series(pan):
    """Clase de serie por SKU (Syntetos-Boylan, T1.5 aprobado 2026-07-27).

    Sobre venta RECURRENTE dentro del span activo (primera→última venta):
      ADI = semanas del span / semanas con venta   (intermitencia)
      CV² = (std/mean)² de las semanas CON venta   (variabilidad del tamaño)
    Cortes estándar (1.32 / 0.49): suave, errática, intermitente, grumosa
    (lumpy). Insumo de la escalera de confianza (lumpy no puede ser ALTA)
    y de la evaluación segmentada de pronósticos.
    """
    sem = np.sort(pan.semana.unique())
    u = (pan.pivot_table(index="codigo", columns="semana", values="unidades_rec",
                         aggfunc="sum").reindex(columns=sem))
    vivo = u.notna().values
    first = vivo.argmax(axis=1)
    last = vivo.shape[1] - 1 - vivo[:, ::-1].argmax(axis=1)
    cols = np.arange(vivo.shape[1])
    interior = (cols >= first[:, None]) & (cols <= last[:, None])
    full = np.where(interior, np.nan_to_num(u.values), np.nan)
    n_span = interior.sum(axis=1)
    nz = np.where(full > 0, full, np.nan)
    n_nz = np.isfinite(nz).sum(axis=1)
    with np.errstate(invalid="ignore"):
        mean_nz = np.nanmean(nz, axis=1)
        std_nz = np.nanstd(nz, axis=1)
        adi = n_span / np.maximum(n_nz, 1)
        cv2 = (std_nz / mean_nz) ** 2
    ok = (n_nz > 0) & (n_span >= 8)
    clase = np.where(adi < 1.32, np.where(cv2 < 0.49, "suave", "errática"),
                     np.where(cv2 < 0.49, "intermitente", "grumosa (lumpy)"))
    out = pd.DataFrame({"codigo": u.index, "adi": adi.round(2), "cv2": cv2.round(2),
                        "clase": np.where(ok, clase, "sin clase")})
    out.to_parquet(os.path.join(DATA, "adi_cv2.parquet"), index=False)
    print(f"  clases de serie: {pd.Series(out.clase).value_counts().to_dict()}", flush=True)
    return out


def main():
    df = cargar()
    df = limpiar(df)
    df = derivar(df)
    asserts_moneda(df)
    pan = construir_panel(df)
    os.makedirs(DATA, exist_ok=True)
    pan.to_parquet(OUT, index=False)
    clasificar_series(pan)
    print(f"\npanel.parquet: {len(pan):,} celdas (SKU,semana) | "
          f"{pan.codigo.nunique():,} SKUs | {pan['semana'].nunique()} semanas", flush=True)
    print(f"  SKUs opinables (>={MIN_SEMANAS} sem): {pan[pan.activo].codigo.nunique():,}", flush=True)
    print(f"  unidades/celda: med {pan.unidades.median():.0f} p90 {pan.unidades.quantile(.9):.0f}",
          flush=True)
    print(f"  precio_lista med {pan.precio_lista.median():.2f} | neto med {pan.neto_prom.median():.2f} "
          f"| margen NETO med {pan.margen.median():.2f}", flush=True)
    print(f"  % líneas especiales (proyecto/negociación): {100*pan.pct_especial.mean():.1f}%", flush=True)


if __name__ == "__main__":
    main()
