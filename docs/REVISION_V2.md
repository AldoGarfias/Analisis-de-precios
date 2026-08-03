# Revisión técnica del motor v2 (elasticidad) — hallazgos

Revisión de `panel.py`, `modelo.py`, `optimizar.py`, `validar.py`, `clusters.py`,
`extract_ventas.py`, `db.py`. Ordenados por impacto en las recomendaciones.

## Críticos (pueden sesgar la DIRECCIÓN de las recomendaciones)

**1. `log1p` atenúa la elasticidad → sesgo sistemático hacia SUBIR.**
La demanda se modela como `log1p(unidades)`. En SKUs de bajo volumen (0–5
unidades/semana, la mayoría en B2B) la compresión es severa: caer de 2→1
unidades es −50% real pero −37% en log1p; de 1→0 es −100% real pero −69%.
|ε| queda subestimada justo donde más SKUs hay. Cadena de consecuencias en
`optimizar.py`: |ε|<1 ⇒ óptimo de esquina ⇒ SUBIR al tope; con re-corrida
mensual y paso de +4pts se produce un trinquete de subidas.
**Fix:** PPML (Poisson con FE, `pyfixest.fepois`). No es cosmético.
**Diagnóstico barato:** event-study por tercil de volumen; si la ε del modelo
se queda corta vs la observada solo en el tercil bajo, es la firma del log1p.

**2. Ceros de stockout contados como "no demanda".**
El panel trata toda semana sin venta como demanda 0 a ese precio. Si el SKU
estaba agotado, el cero no informa del precio. Peor: los quiebres suelen
coincidir con subidas de costo/lista (escasez) ⇒ infla |ε| espuriamente y
rompe la restricción de exclusión del instrumento (costo). El pipeline nunca
consulta existencias (solo `publicar.py`, al final). Los SKUs descontinuados a
mitad de ventana arrastran ceros hasta el final (el recorte es solo pre-primera
venta). **Fix:** dummy/exclusión de semanas sin inventario; recorte post-última
venta con criterio de descontinuación.

**3. Selección por signo en dos lugares.**
(a) `modelo.py`: el ε propio del SKU solo entra al blend EB si salió negativo
(`if own and own[0] < 0`); los positivos (ruido en la otra dirección) se
descartan ⇒ el combinado queda sesgado hacia más negativo.
(b) `validar.py`: el event-study exige ventas>0 antes Y después del cambio;
excluye los casos donde subir el precio mató la venta ⇒ subestima la respuesta
observada (sesgo de supervivencia) justo en la prueba de validación.

## Moderados (distorsionan magnitudes)

**4. `bfill` fabrica historia con precios del futuro.**
`_asof_por_sku` rellena semanas previas al primer registro con el primer valor
conocido (posterior en el tiempo). Además `extract_ventas.py` filtra historial a
`fecha >= 2024-01-01`: un SKU sin cambios desde entonces no tiene filas ⇒ p1
NaN en todo el panel ⇒ el dropna lo ELIMINA del pipeline en silencio (no sale
como MANTENER: desaparece de recomendaciones.csv).
**Fix:** sembrar cada SKU con el último precio anterior a la ventana (o el
precio_1 actual si nunca cambió); forward-fill only.

**5. u0 anclada al promedio de 12 meses.**
`optimizar.py` proyecta `u0·f^ε` con el promedio anual, vendido a precios
históricos distintos del actual y ciego a tendencia (un SKU en declive usa su
"yo" de hace 10 meses). **Fix:** promedio de últimas 8–12 semanas o pronóstico
del modelo al precio actual (→ `forecast.py`).

**6. Moneda sin verificar.**
`costo` y `precio_real` en USD; `p1` de `historial_precios_asteriscos` sin
conversión. Si alguna marca lista en pesos, `rho ≈ 0.05` y el `clip(0.3, 1.2)`
lo DISFRAZA como 0.3 en vez de reventar ⇒ margen/utilidad corruptos en
silencio. **Fix:** assert por `moneda` antes de optimizar; el clip no debe
tapar errores de escala.

**7. `rho` estático y desfasado.**
Mediana del realizado de 12 meses contra el p1 de HOY; tras un cambio de lista
reciente queda mal, y asume pass-through constante (si al subir lista los
vendedores descuentan más, el impacto de SUBIR está sobreestimado).

## Menores / ingeniería

**8. Bypass del candado de escritura (`db.py`).**
El regex valida que la sentencia EMPIECE en `precio_optimo_*`, pero MySQL
permite multi-tabla: `UPDATE precio_optimo_x JOIN cat_articulos2 ... SET
cat_articulos2.precio_1=...` pasa el candado. **Fix:** no validar SQL crudo;
exponer funciones que construyan el SQL internamente, o rechazar JOIN/coma en
la cláusula de tabla.

**9. `_demean2` con 4 iteraciones fijas** puede no converger en panel muy
desbalanceado. **Fix:** iterar hasta tolerancia.

**10. Cross-price casi decorativo en el piloto.** Sustitutos limitados al
universo de marcas piloto; `log_psust` rellenado con 0 y a menudo sin varianza
tras demean. Componentes conexas pueden encadenar clusters gigantes que
diluyen el τ² del EB.

**11. Umbral de confianza generoso.** "alta" con se≤0.4: con ε≈−1.2 el IC90%
cruza la frontera elástico/inelástico, que es justo la que decide esquina vs
interior en el optimizador.

## Lo que está bien (conservar)

Palanca correcta (lista, no descuento); FE de dos vías con SE clusterizados;
IV condicionado a fuerza del instrumento (F≥10); abstención sobre estimación
mala; event-study como concepto de validación; escritura acotada con
transacción controlada por el llamador; esquema de salida compatible con
publicar.py; guardrails idénticos entre motores.

## Top-3 si solo se puede arreglar tres cosas

1. PPML en vez de log1p (elimina el sesgo direccional).
2. Existencias en el panel (limpia ε y el instrumento).
3. Sembrado del precio inicial (reaparecen los SKUs de precio estable).
