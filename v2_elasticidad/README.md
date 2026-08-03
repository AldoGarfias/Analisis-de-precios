# Motor v2 — Precio óptimo por elasticidad de la demanda real

Reemplazo del motor v1 (win-rate de cotizaciones) porque aquél **se basa en
cotizaciones**: el descuento cotizado es endógeno, mezcla precio con stock/entrega
y con cotizaciones web a lista, y no ve demanda perdida. Este motor mide la
**elasticidad causal de la venta real** al precio de lista (la palanca del panel).

## Idea en una frase
¿Cuánto cambian las **unidades vendidas** cuando movemos el **precio de lista**?
Con esa elasticidad, el precio óptimo es el que maximiza `unidades × margen`.

## Cómo se estima (identificación > modelo)

1. **Panel SKU × semana** (`panel.py`), 12 meses. Unidades reales (incluye semanas
   con 0 ventas, informativas), precio de lista `p1` como serie escalonada
   (`historial_precios_asteriscos`), costo USD (`costos_prom`), precio realizado y
   FX real (`art_vnts_por_mes`), y el precio del sustituto más cercano (cross-price).

2. **Efectos fijos de SKU + de mes** (`_demean2`): la elasticidad se identifica de
   la variación de precio **dentro del mismo SKU en el tiempo**, no de comparar SKUs
   caros vs baratos. El FE de mes controla estacionalidad/macro. (El FX macro es un
   puro efecto de tiempo → queda absorbido por el FE; por eso el instrumento útil es
   el costo específico del SKU, no el FX.)

3. **Estimador** (`modelo.py`, `_fe_iv`):
   - **FE-OLS** es el primario. Clave: la palanca es el **precio de LISTA**, que el
     PM administra por costo/estrategia — mucho menos endógeno a la demanda de la
     semana que un descuento por trato. Por eso FE-OLS sobre lista es defendible.
   - **IV con costo** (`cos_prom_dlls`) como refinamiento: si el costo es instrumento
     fuerte (F de primera etapa ≥ 10) se usa IV para quitar sesgo residual; si es
     débil, se reporta FE-OLS y se marca la confianza.
   - Se estima por **marca** (robusto) y por **cluster de similares**; el ε de cada
     SKU se encoge (**Empirical Bayes**) hacia su cluster con su propia señal.
   - **Guardas de honestidad**: ε debe ser < 0 y (instrumento fuerte **o** pendiente
     significativa). Si no, el SKU queda a baja confianza → el optimizador lo deja en
     **MANTENER**. El motor prefiere no opinar antes que opinar mal.

4. **Óptimo** (`optimizar.py`): barre el multiplicador de precio dentro de los
   guardrails (`utilidad = u0·f^ε · (rho·p1·f − costo)`) y toma el máximo.
   Elástico (|ε|>1) → hay interior; inelástico (|ε|<1) → subir al tope.
   Guardrails idénticos al v1: piso margen costo+3pts, factor ≥1.6, ±10pts máx,
   paso mes ±4pts. **Sobrestock manda** (lo aplica `publicar.py` con existencias).

## Validación (`validar.py`) — ¿funciona?
- **Diagnóstico por marca**: F de instrumento, signo de ε, identificación.
- **Event-study** (la prueba real): sobre cambios de precio **reales** compara la
  elasticidad observada (Δlog unidades / Δlog precio, ±4 sem) vs la ε del modelo.
- **Estabilidad out-of-time**: ε en train 80% vs completo.

## Correr
```
python3 run.py                          # marcas piloto, extrae de BD + corre todo
python3 run.py HIKVISION,PANDUIT        # marcas específicas
python3 run.py --skip-extract           # reusa parquet (sin tocar BD)
```
Salida: `out/recomendaciones.csv` (mismo esquema que el v1 → `publicar.py` lo
consume sin cambios) + `out/validacion_elast.txt`.

## Dependencias de integración con publicar.py (v1)
- `publicar.py` lee `data/articulos.parquet` (**catálogo completo**, producido por el
  `extract.py` del v1). Correr `python3 extract.py` una vez antes de `publicar.py` si
  ese archivo no existe. Este motor escribe `data/elast/articulos.parquet` (filtrado
  por marca) y NO debe sobrescribir el catálogo completo.
- `optimizar.py` emite `id_asterisco=0` (el precio de lista base vive en el asterisco
  0, y ~91% de las existencias están ahí). El guard de sobrestock de `publicar.py`
  **subcuenta** el stock de SKUs con inventario en asteriscos ≠0 (~9%). Fix pendiente
  (fuera de alcance, tocaría publicar.py del v1): que `existencias()` sume existencia
  por `id_art` a través de asteriscos.

## Limitaciones honestas (qué NO puede)
- **No observa demanda perdida** (quien no compró) ni precio de competencia.
- Si un SKU/marca **no mueve su precio de lista** en la ventana, no hay de dónde
  identificar elasticidad → queda MANTENER a baja confianza. Es correcto, no un bug.
- Sólo 12 meses (rolling de `art_vnts_por_mes`); más historial mejora la precisión.
- **Identificación frágil / dependiente de especificación** con este dato: en el
  piloto sólo PANDUIT (commodity) resultó identificable, y su ε es modesta y
  sensible (se mueve al cambiar la ventana o los controles). HIKVISION/UBIQUITI/
  LINKEDPRO no se identifican (suben precio a lo que ya vende). Por eso el camino
  robusto es EXPERIMENTAL: los pasos ±4pts del panel generan la variación exógena.
- Pass-through modelado como nivel (realizado = ρ·lista); κ<1 fino queda para v3.

## Ruta de mejora (fuera de alcance ahora)
- **PPML** (Poisson con FE de alta dimensión, p.ej. `pyfixest.fepois`) en vez de
  log1p-OLS para tratar los ceros de forma exacta.
- **Bayes jerárquico** completo (intervalos de credibilidad por SKU).
- **Elasticidad cruzada** multi-SKU (sistema de demanda por cluster) y
  heterogeneidad por canal/cliente (Double ML / EconML).
- Señal web (vistas / carrito sin compra) como proxy de demanda perdida.
