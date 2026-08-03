# Precio Óptimo — cómo se calcula

Modelo de **respuesta al precio (win-rate / bid-response)**, el estándar de la
industria para pricing B2B. En una frase: estimamos la probabilidad de ganar
una cotización a cada precio, y elegimos el precio que maximiza la utilidad
esperada (probabilidad de ganar × margen × volumen).

Dos scripts:
- **`model.py`** — entrena el modelo de probabilidad de ganar.
- **`recommend.py`** — con ese modelo, calcula el precio óptimo por producto.

---

## 1. Datos y etiqueta

- Universo: cada **renglón de cotización** de los últimos ~13 meses (~4 millones).
- **Etiqueta (lo que predecimos):** ¿esa cotización se convirtió en pedido?
  (campo `fecha_pedido`). ~25-31% de los renglones convierten.
- **Variable de decisión:** `precio_relativo = precio cotizado / precio de lista`
  (mediana ~0.89; hay variación real de descuento para poder estimar la curva).
- **Costo:** costo promedio real por artículo (fallback al factor de catálogo).

## 2. El modelo — P(ganar | precio, contexto)

`HistGradientBoostingClassifier` de scikit-learn (árboles con *gradient
boosting*). Predice la probabilidad de ganar la cotización en función de:

- el **precio relativo** y su desviación vs el nivel típico de ese producto,
- **contexto**: línea, marca, proveedor, clasificación de cliente, sucursal,
  cantidad, monto, mes.

Dos decisiones clave de calidad:
- **Restricción monotónica**: al modelo se le *prohíbe* aprender que subir el
  precio aumenta la probabilidad de ganar. Garantiza curvas con sentido
  económico (a más precio, menos probabilidad de ganar).
- **Modelo probabilístico calibrado** (no un sí/no): permite graficar la curva
  completa P(ganar) vs precio de cualquier producto.

**Validación honesta (out-of-time):** se entrena con datos hasta mayo-2026 y se
evalúa contra junio/julio-2026 (como operaría en la realidad). **AUC 0.79** y
calibración casi perfecta (cuando dice "20% de probabilidad", en la realidad
gana ~20%).

## 3. El óptimo — `recommend.py`

Para cada producto, sobre sus renglones reales de los últimos 90 días:

1. Se **barre el precio** en una malla (de 0.55× a 1.04× del precio de lista).
2. En cada punto se calcula la **utilidad esperada**:

   ```
   utilidad(precio) = P(ganar | precio) × (precio − costo) × volumen
   ```

3. Se elige el precio que **maximiza** esa utilidad esperada.

**Guardrails (topes de seguridad):**
- Piso de margen: el precio nunca baja de costo + 3 puntos.
- Movimiento máximo de ±10 puntos vs el nivel actual.
- **Paso del mes: ±4 puntos** — mejor práctica: mover poco, re-medir y repetir
  (el óptimo completo queda como referencia, no se aplica de golpe).
- Mínimo 20 cotizaciones en 90 días para opinar sobre un producto.

**Sobrestock manda:** si un producto tiene ≥12 meses de inventario, no se sube
el precio (no tiene sentido encarecer lo que no rota).

## 4. Resultado

Por producto: **SUBIR / BAJAR / MANTENER**, el precio sugerido en pesos/dólares,
el paso prudente del mes, y el **impacto estimado** (utilidad esperada extra por
mes). Cada mes se re-corre y se compara contra el mes anterior para ver qué
recomendaciones son nuevas y cuáles ya se atendieron.

---

## Hallazgo de negocio que salió del modelo

En clientes **distribuidor**, bajar el precio más de ~5 puntos por debajo del
nivel típico del producto **no aumenta la conversión** (la probabilidad de ganar
se aplana). Es decir, los descuentos muy profundos regalan margen sin comprar
más ventas. Donde sí hay sensibilidad al precio es en el precio de lista completo
y en ciertos productos/segmentos concretos.

## Notas técnicas

- Es **datos tabulares** → *gradient boosting* le gana a las redes neuronales
  (y es lo que usan los motores comerciales de pricing B2B tipo Zilliant/PROS).
- Los scripts operan sobre archivos `parquet` locales (extraídos aparte con
  solo-lectura de la base). **No contienen credenciales ni conexiones.**
- Dependencias: `scikit-learn`, `duckdb`, `pandas`, `numpy`, `joblib`.
