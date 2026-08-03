# -*- coding: utf-8 -*-
"""ETAPA 2 — WIN-RATE: P(la cotización se convierte en venta | precio ofrecido
relativo a la lista aplicable). Primer entregable aprobado 2026-08-01
("Aplica las 4", punto 4): la TASA DE ÉXITO POR NIVEL DE PRECIO — la evidencia
que, según la literatura de fuerza de ventas, reduce el sobre-descuento
preventivo del vendedor (docs/ANALISIS_CANAL_LINEA.md).

DEFINICIONES FIJADAS ANTES DE VER RESULTADOS:
  - Observación = negociación: dedup CLIENTE-SKU-SEMANA (lección v1 #2),
    conservando la ÚLTIMA cotización de la semana (la oferta final).
  - rel_precio = neto ofrecido / lista aplicable. En reporte_61 el `precio`
    del renglón ES la lista aplicable a su `tipo_precio` (1 ó 3) — lección
    v1 #3 resuelta por construcción; neto = subtotal/cantidad (stack completo
    de descuentos verificado).
  - GANADA = existe venta Activa del MISMO cliente+SKU en los 28 días
    siguientes a la cotización (incluido el día).
  - Peso = 1/renglones-del-folio en la semana (lección v1 #1: una cotización
    de 50 líneas no debe pesar 50 negociaciones).
  - Higiene del panel: tipo_precio ∈ {1,3}, precio>0, cantidad>0, sin kits,
    sin conceptos de proyecto.

Salidas: data/winrate_dataset.parquet (observación-negociación) +
data/winrate_curva.parquet (curva por bucket de descuento, global y por canal)
+ impresión del estudio. NO cambia reglas del motor: es el cimiento del
modelo bid-response (GBM monotónico) y de la evidencia al vendedor.
"""
import os

import duckdb

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
R61 = os.path.join(DATA, "reporte61")
VENTANA_DIAS = 28


def correr():
    con = duckdb.connect()
    con.execute(f"SET threads TO 6")

    q = f"""
    WITH cot AS (
      SELECT CAST(fecha AS DATE) AS fecha, folio, codigo, codigo_cliente,
             TRY_CAST(cantidad AS DOUBLE) AS cantidad,
             TRY_CAST(precio AS DOUBLE) AS precio,
             TRY_CAST(tipo_precio AS INT) AS tipo_precio,
             TRY_CAST(subtotal AS DOUBLE) AS subtotal,
             lower(concepto) AS conc, via
      FROM read_parquet('{R61}/cotiz_*.parquet')
      WHERE kit != 'Si'
        AND TRY_CAST(precio AS DOUBLE) > 0
        AND TRY_CAST(cantidad AS DOUBLE) > 0
        AND TRY_CAST(tipo_precio AS INT) IN (1, 3)
        AND lower(concepto) NOT LIKE '%royecto%'
    ),
    cot2 AS (
      SELECT *,
             subtotal / cantidad / precio AS rel_precio,
             date_trunc('week', fecha) AS semana,
             -- l_nea: el API devuelve "l?nea" (í rota) — jamás filtrar por acentos
             CASE WHEN conc LIKE '%l_nea%'
                  THEN 'linea' ELSE 'vendedor' END AS canal
      FROM cot
      WHERE subtotal / cantidad / precio BETWEEN 0.30 AND 1.10
    ),
    -- dedup cliente-SKU-semana: la ÚLTIMA oferta de la semana
    neg AS (
      SELECT * EXCLUDE (rn) FROM (
        SELECT *, row_number() OVER (
          PARTITION BY codigo_cliente, codigo, semana
          ORDER BY fecha DESC, rel_precio ASC) AS rn
        FROM cot2)
      WHERE rn = 1
    ),
    -- peso: 1 / renglones del folio (tras dedup)
    negw AS (
      SELECT *, 1.0 / COUNT(*) OVER (PARTITION BY folio) AS w FROM neg
    ),
    vta AS (
      SELECT DISTINCT codigo_cliente, codigo, CAST(fecha AS DATE) AS fv
      FROM read_parquet('{R61}/ventas_*.parquet')
      WHERE TRY_CAST(precio AS DOUBLE) > 0
        AND TRY_CAST(cantidad AS DOUBLE) > 0
    )
    SELECT n.*, (EXISTS (
             SELECT 1 FROM vta v
             WHERE v.codigo_cliente = n.codigo_cliente AND v.codigo = n.codigo
               AND v.fv BETWEEN n.fecha AND n.fecha + INTERVAL {VENTANA_DIAS} DAY
           )) AS ganada
    FROM negw n
    """
    df = con.execute(q).df()
    ruta = os.path.join(DATA, "winrate_dataset.parquet")
    df.to_parquet(ruta, index=False)
    print(f"negociaciones (dedup cliente-SKU-semana): {len(df):,} | "
          f"win-rate crudo: {df.ganada.mean():.1%} | "
          f"ponderado: {(df.ganada * df.w).sum() / df.w.sum():.1%}", flush=True)

    # ── CURVA: win-rate por profundidad de descuento (1 − rel_precio) ──
    import numpy as np
    import pandas as pd
    df["desc_pct"] = (1 - df.rel_precio) * 100
    cortes = [-1, 15, 18, 20, 22, 25, 30, 40, 71]
    labels = ["<15%", "15-18", "18-20", "20-22 (estándar)", "22-25",
              "25-30", "30-40", ">40%"]
    df["bucket"] = pd.cut(df.desc_pct, cortes, labels=labels)
    filas = []
    for scope, d in [("global", df)] + [(c, df[df.canal == c])
                                        for c in ("linea", "vendedor")]:
        g = d.groupby("bucket", observed=True).apply(
            lambda x: pd.Series({
                "n": len(x),
                "win": (x.ganada * x.w).sum() / max(x.w.sum(), 1e-9),
            }), include_groups=False).reset_index()
        g["scope"] = scope
        filas.append(g)
    C = pd.concat(filas, ignore_index=True)
    C.to_parquet(os.path.join(DATA, "winrate_curva.parquet"), index=False)

    print("\n== TASA DE ÉXITO POR PROFUNDIDAD DE DESCUENTO (28 días, ponderada) ==",
          flush=True)
    for scope in ("global", "linea", "vendedor"):
        g = C[C.scope == scope]
        print(f"  {scope}:", flush=True)
        for _, x in g.iterrows():
            barra = "█" * int(50 * x.win)
            print(f"    {str(x.bucket):<18} n={int(x.n):>8,}  {x.win:6.1%}  {barra}",
                  flush=True)
    return df


def paquete():
    """CIERRE EN PAQUETE (usuario 2026-08-01): un folio de cotización con n
    SKUs se considera cerrado si existe un folio de FACTURA del mismo cliente
    (≤28 días) cuyos SKUs son EXACTAMENTE los cotizados, tolerando hasta 20%
    de SKUs menos (el cliente quitó productos al final). Regla:
      SKUs_factura ⊆ SKUs_cotización  Y  |∩| ≥ 0.80 × n_cotizados.
    Se reporta win-rate de PAQUETE por tamaño de folio y por profundidad de
    descuento del folio. Salida: data/winrate_paquete.parquet."""
    import pandas as pd
    con = duckdb.connect()
    con.execute("SET threads TO 6")
    q = f"""
    WITH cl AS (  -- renglones de cotización (folio, SKUs únicos)
      SELECT DISTINCT folio AS fc, codigo_cliente AS cli, codigo,
             MIN(CAST(fecha AS DATE)) OVER (PARTITION BY folio) AS fecha_c
      FROM read_parquet('{R61}/cotiz_*.parquet')
      WHERE kit != 'Si' AND TRY_CAST(precio AS DOUBLE) > 0
        AND TRY_CAST(cantidad AS DOUBLE) > 0
        AND TRY_CAST(tipo_precio AS INT) IN (1, 3)
        AND lower(concepto) NOT LIKE '%royecto%'
    ),
    fq AS (SELECT fc, cli, fecha_c, COUNT(*) AS nq FROM cl GROUP BY ALL),
    vl AS (  -- renglones de factura (folio, SKUs únicos)
      SELECT DISTINCT folio AS ff, codigo_cliente AS cli, codigo,
             MIN(CAST(fecha AS DATE)) OVER (PARTITION BY folio) AS fecha_f
      FROM read_parquet('{R61}/ventas_*.parquet')
      WHERE TRY_CAST(precio AS DOUBLE) > 0 AND TRY_CAST(cantidad AS DOUBLE) > 0
    ),
    fv AS (SELECT ff, cli, fecha_f, COUNT(*) AS nf FROM vl GROUP BY ALL),
    -- pares folio_cot × folio_fact con SKUs en común (mismo cliente, ventana)
    comun AS (
      SELECT c.fc, v.ff, COUNT(*) AS ncom
      FROM cl c JOIN vl v
        ON v.cli = c.cli AND v.codigo = c.codigo
       AND v.fecha_f BETWEEN c.fecha_c AND c.fecha_c + INTERVAL 28 DAY
      GROUP BY ALL
    ),
    match AS (
      SELECT m.fc, m.ff, m.ncom, q.nq, f.nf,
             row_number() OVER (PARTITION BY m.fc
                                ORDER BY m.ncom DESC, f.fecha_f) AS rn
      FROM comun m
      JOIN fq q ON q.fc = m.fc
      JOIN fv f ON f.ff = m.ff
      WHERE f.nf = m.ncom                -- la factura NO trae SKUs ajenos
        AND m.ncom >= CEIL(0.80 * q.nq)  -- cubre ≥80% de lo cotizado
    )
    SELECT q.fc, q.cli, q.fecha_c, q.nq,
           m.ff, m.ncom, m.nf,
           (m.fc IS NOT NULL) AS cerrado_paquete
    FROM fq q LEFT JOIN (SELECT * FROM match WHERE rn = 1) m ON m.fc = q.fc
    """
    P = con.execute(q).df()
    # profundidad de descuento del folio (ponderada por subtotal)
    d = con.execute(f"""
      SELECT folio AS fc,
             1 - SUM(TRY_CAST(subtotal AS DOUBLE))
               / SUM(TRY_CAST(precio AS DOUBLE) * TRY_CAST(cantidad AS DOUBLE)) AS desc_fol
      FROM read_parquet('{R61}/cotiz_*.parquet')
      WHERE kit != 'Si' AND TRY_CAST(precio AS DOUBLE) > 0
        AND TRY_CAST(cantidad AS DOUBLE) > 0
        AND TRY_CAST(tipo_precio AS INT) IN (1, 3)
        AND lower(concepto) NOT LIKE '%royecto%'
      GROUP BY folio
    """).df().set_index("fc").desc_fol
    P["desc_fol"] = P.fc.map(d)
    P.to_parquet(os.path.join(DATA, "winrate_paquete.parquet"), index=False)

    import numpy as np
    print(f"folios cotizados: {len(P):,} | cerrados EN PAQUETE (⊆ y ≥80%): "
          f"{P.cerrado_paquete.mean():.1%}", flush=True)
    P["tam"] = pd.cut(P.nq, [0, 1, 2, 5, 10, 25, 10**6],
                      labels=["1 SKU", "2", "3-5", "6-10", "11-25", ">25"])
    print("\n  por tamaño del folio cotizado:", flush=True)
    for t, g in P.groupby("tam", observed=True):
        print(f"    {str(t):<7} n={len(g):>9,}  cierra paquete: {g.cerrado_paquete.mean():6.1%}"
              f"  (cobertura mediana del match: "
              f"{(g[g.cerrado_paquete].ncom / g[g.cerrado_paquete].nq).median() if g.cerrado_paquete.any() else float('nan'):.0%})",
              flush=True)
    print("\n  por descuento del folio (solo folios de ≥2 SKUs):", flush=True)
    m = P[P.nq >= 2].copy()
    m["b"] = pd.cut(100 * m.desc_fol, [-1, 20, 30, 40, 100],
                    labels=["≤20%", "20-30", "30-40", ">40%"])
    for b, g in m.groupby("b", observed=True):
        print(f"    {str(b):<6} n={len(g):>9,}  cierra paquete: {g.cerrado_paquete.mean():6.1%}",
              flush=True)
    return P


if __name__ == "__main__":
    import sys as _sys
    modo = _sys.argv[1] if len(_sys.argv) > 1 else ""
    {"paquete": paquete}.get(modo, correr)()
