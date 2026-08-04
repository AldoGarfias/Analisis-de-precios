# Motor de Precio Óptimo B2B — v3 (nueva base de datos en AWS)

## Qué es este proyecto

Nueva iteración de un motor de pricing B2B. Existen dos motores previos (incluidos
como referencia en este repo) construidos sobre el ERP anterior:

- **`v1_cotizaciones/`** — modelo win-rate / bid-response: P(ganar cotización |
  precio relativo a lista, contexto) con GBM monotónico. Optimiza la **política de
  descuento** por SKU. Ya incluye 3 correcciones aplicadas y probadas:
  (1) peso proporcional por cotización (`sample_weight`, canal `via` + lista completa),
  (2) dedup semanal cliente-SKU-semana (una negociación = una observación),
  (3) `rel_precio` contra la **lista aplicable** (precio 1 vs precio 3 según
  `tipo_precio`), no siempre contra la lista alta.
- **`v2_elasticidad/`** — elasticidad causal de la venta real al **precio de lista**
  (panel SKU×semana, FE de dos vías, IV con costo, Empirical Bayes). Optimiza el
  **precio de lista**. Revisado a fondo: ver `docs/REVISION_V2.md` (11 hallazgos).

La meta del v3: reconstruir el pipeline sobre la nueva base de datos incorporando
las lecciones de ambos. Los dos motores son **complementarios** (palancas
distintas: lista ↔ PM, descuento ↔ vendedor), no excluyentes.

## Estado actual (v3 en marcha — actualizado 2026-08-01)

- [x] **Motor (2026-07-31)**: TITULAR = API de BI (developers.syscom.mx → Redshift,
      sin VPN, `api_bi.py`/`extract_api.py`); SUPLENTE = Aurora MySQL 5.7 vía VPN
      (`db.py`/`extract.py`, fallback automático). Validación espejo +0.000%.
      Reglas de migración: NULL→'', jamás filtrar por caracteres acentuados.
      Recomendaciones se guardan LOCAL en `out/` (fase de pruebas, sin escritura a BD).
- [x] **Fuente ÚNICA**: `reportes.reporte_61` (~72M renglones; Redshift trae 2016→hoy; VENTANA OPERATIVA del panel: 2024→hoy, decidida 2026-07-31).
      SKU = `codigo`. NO usar `marca` ni `linea`. Estatus: Activa/Cotizacion/
      Nota credito/Cancelada. Solo `tipo_precio` ∈ {1,3} (1=lista, 3=oferta);
      `precio=0` = componente de kit (excluido). `costo_venta` es costo de LÍNEA
      (unitario = costo_venta/cantidad). Stack de descuentos multiplicativo
      verificado: `neto = precio·(1-d1)(1-d2)(1-desc_fin/100)`; margen sobre NETO.
- [x] **Moneda**: precio/subtotal/costo en USD (misma escala); `tipo_cambio` por
      renglón para MXN. Asserts activos en `panel.py`.
- [x] `via` existe (canal) para los pesos del v1.
- **Pipeline actual** (`run.py`, 8 pasos): `panel.py` → `modelo.py` (ε PPML con
  ceros y stockouts; las ε VIGENTES viven en `data/elasticidad_sku.parquet` y
  `data/eps_por_sku.parquet` — NO citar valores congelados, se re-estiman cada
  corrida) → `analisis_eps_sku.py aplicar` (capa SKU EB por eventos propios) →
  `validar.py` → `forecast.py` (u0 = GBM residual, campeón en backtest) →
  `analisis_canasta.py anclas` (mapa de canasta) → `analisis_canal.py mezcla`
  (mezcla en línea/vendedor) → `escenarios.py` (árbol de
  decisión completo → `out/recomendaciones.csv`, `out/escenarios.csv`).
  Aparte: `extract_api.py`/`extract.py` (ventas/existencias/kits/proveedores),
  `dormidos.py` (2ª capa), `valida_decisiones.py` (calibración),
  `seguimiento_frenos.py` (cron diario 8:30), `genera_paneles.py` (cron 0:00),
  `reporte_top.py`/`panel_sku.py` (vistas HTML).
- **LA REFERENCIA AUTORITATIVA de arquitectura, árbol de decisión, capas y
  reglas de negocio es `docs/ARQUITECTURA_V3.md`** — leerla antes de modificar
  cualquier regla; se deriva del código real y se actualiza con él.
- Etapa 2 INICIADA 2026-08-01: cotizaciones extraídas (`extract_cotizaciones.py`,
  32 meses) y dataset win-rate construido (`winrate.py`); pendiente el modelo
  bid-response (GBM monotónico) y `Nota credito`,
  EB por SKU (escalera de granularidad de ε), DML (triangulación causal),
  ventana 2018+ para estacionalidad.

## Reglas de seguridad de base de datos (innegociables)

1. **Credenciales solo en `.env.local`** (junto a `db.py`, en `.gitignore`).
   Nunca en el código, nunca en commits, nunca pegadas en conversaciones.
2. **Lectura por defecto**: usuario de solo-SELECT, idealmente contra réplica de
   lectura. El wrapper `query()` debe rechazar cualquier verbo que no sea
   SELECT/SHOW/DESCRIBE/EXPLAIN.
3. **Escritura acotada** a tablas propias del proyecto (`precio_optimo_*`).
   OJO: el candado regex del v2 (`db.py`) tiene un **bypass**: un
   `UPDATE precio_optimo_x JOIN otra_tabla SET otra_tabla.col=...` pasa la
   validación. En el v3 NO validar SQL arbitrario con regex: exponer funciones
   que construyan el SQL internamente (p.ej. `guardar_recos(df)`) y prohibir
   JOIN/coma en la cláusula de tabla si se acepta SQL crudo.
4. **El pipeline nunca escribe precios reales** al catálogo. Los cambios de precio
   se aplican solo por el flujo auditado del ERP.

## Decisiones de diseño que se conservan (no re-litigar)

- Palanca para elasticidad = **precio de LISTA** (administrado, poco endógeno);
  el descuento por trato es endógeno y solo se modela con el enfoque win-rate.
- **Guardrails**: piso de margen costo+3pts, movimiento máx +10pts ACUMULADOS en ventana móvil de 12 meses con re-ancla por costo real ≥5% (IMPLEMENTADO 2026-08-04; se activa cuando el registro de cambios aplicados tenga filas — los cambios aplicados llevan etiqueta 'Motor de Precios v3 | ciclo ...' en el campo comentario del ERP), paso máximo
  ±4pts POR CICLO DE 3 SEMANAS (cadencia definida por el negocio:
  un precio nunca se mueve dos semanas seguidas; aplicar poco → re-medir →
  repetir), mínimo de actividad para opinar, **sobrestock manda** (≥12 meses de
  stock ⇒ no subir; con margen, BAJAR para rotar). Meses de stock (v3, 2026-07-31) = stock de VENTA ÷ demanda esperada: forecast
  propio mensual (ensamble GBM+ingenuo) con filtro de credibilidad; respaldo =
  venta real sin meses de cero-por-stockout + tasa de proyectos a ventana larga.
  Ventas de proyecto NO cuentan como demanda DE PRECIO (señales sin cambio),
  pero SÍ rotan almacén.
- **Preferir abstención a opinar mal**: sin identificación limpia ⇒ MANTENER
  a baja confianza. Es una feature, no un bug.
- Validación out-of-time siempre (entrenar pasado, evaluar futuro) y, para
  elasticidad, event-study sobre cambios de precio reales.
- Los pasos mensuales de ±4pts son la fuente de variación cuasi-experimental:
  registrar cada cambio aplicado (fecha, SKU, precio antes/después) desde el día 1
  para que las estimaciones futuras puedan usarlos.

## Prioridades técnicas del v3 (orden recomendado)

1. **PPML en lugar de log1p-OLS** para la demanda (p.ej. `pyfixest.fepois`).
   El log1p atenúa |ε| en SKUs de bajo volumen ⇒ sesgo sistemático hacia SUBIR
   (hallazgo #1). Es el fix de mayor impacto por línea de código.
2. **Existencias en el panel**: marcar/excluir semanas de stockout (un cero sin
   inventario no informa del precio y contamina el instrumento de costo) y
   recortar SKUs descontinuados a mitad de ventana (hallazgo #2).
3. **Sembrado correcto del precio inicial**: último cambio ANTERIOR a la ventana
   (forward-fill only; jamás bfill con valores del futuro). Extraer historial sin
   filtro de fecha o con fallback al precio actual para SKUs sin cambios — hoy
   esos SKUs desaparecen del pipeline en silencio (hallazgo #4).
4. **`forecast.py`**: pronóstico de demanda base u0 con gradient boosting
   (lags, tendencia, mes — NUNCA marca/línea) validado out-of-time contra el promedio
   simple; si no le gana, no entra. Sustituye el u0 = promedio de 12 meses
   (hallazgo #5) y reintroduce sensibilidad a tendencia.
5. **`dml.py`**: Double ML (residuo-sobre-residuo con cross-fitting, GBM para
   los nuisances) como TERCER estimador de ε junto a FE-OLS e IV.
   Regla de triangulación: los tres coinciden en signo y magnitud ⇒ confianza
   alta; divergen ⇒ MANTENER. ML nunca estima ε directo de predicción
   (recogería la endogeneidad demand-driven).
6. Quitar la selección por signo del ε propio del SKU en el blend EB
   (hallazgo #3a) y corregir el sesgo de supervivencia del event-study (#3b).
7. Asserts de moneda y de `rho` fuera del clip antes de optimizar (#6, #7).

## Convenciones del repo

- Python 3.11+, `pandas`/`numpy`/`duckdb` para datos; econometría con
  `linearmodels` (+ `pyfixest` para PPML); ML con `scikit-learn`
  (HistGradientBoosting; XGBoost solo si el equipo lo prefiere — equivalente).
- Datos crudos extraídos a `data/*.parquet` (nunca commiteados), salidas a `out/`.
- Todo script imprime conteos/sanity checks al correr (patrón de v1 y v2).
- Comentarios y docs en español; nombres de columnas del ERP se respetan.

## Cómo empezar una sesión típica

1. Lee `docs/REVISION_V2.md` (hallazgos completos) y los README de v1/v2.
2. Pide al usuario el motor y esquema de la nueva BD si aún no están definidos.
3. Adapta `db.py` (driver + candados) y escribe el `extract.py` del nuevo esquema.
4. Reconstruye panel → modelo → optimizador aplicando las prioridades de arriba,
   corriendo sanity checks contra datos reales en cada paso.
