# CAMBIOS PARA TI — bitácora de actualización del motor

Registro de TODO cambio de reglas y código desde la entrega inicial, en
formato listo para que TI actualice su copia. **Cada entrada = un commit de
git**; la forma más segura de actualizar es `git pull` (o pedir el
`git diff <hash_anterior> <hash_nuevo>`). Este documento es el índice legible.

Formato de cada entrada:
- **Qué cambió** (regla de negocio o código) y **por qué** (decisión/fecha)
- **Archivos tocados** y dónde
- **Cómo actualizar** si no usan git (qué copiar/reemplazar)

Línea base de la entrega: commit `43ebb46` (2026-08-04, "entrega inicial,
auditado ronda 4"). El zip `motor-precios-v3-entrega.zip` corresponde a
`8dbc29d` (incluye TODO lo de abajo).

---

## (este commit) — 2026-08-10 · SEMÁFORO COMPETITIVO (aprobado por el usuario)

### NUEVO: `semaforo.py` — valoración con 4 firmas de evidencia
- **Qué**: clasifica cada par de competencia ANTES de que nadie reaccione a un
  precio ajeno: (1) persistencia del precio (≥7d), (2) trayectoria de SU stock
  (vendiendo/reponiendo/estancado), (3) consenso (nº fuentes ≤+5% del mejor),
  (4) NUESTRO daño (crecimiento de venta del modelo). Clasificación:
  AMENAZA REAL (gap<-10 + persistente + su stock rotando + nuestra venta
  cayendo) / REMATE AJENO (barato pero atorado ⇒ ignorar) / ESPACIO (estamos
  ≥10% abajo, persistente ⇒ respalda subir) / NEUTRO. EXACTOS con candados
  (marca coincide + |gap|≤60) son la voz firme; EQUIVALENTES solo contexto
  con su confiabilidad (sim del vector). 1ª corrida: 2,664 pares → 18 AMENAZA
  REAL, 421 remate ajeno, 639 espacio. Salidas: semaforo.parquet +
  semaforo_modelo.parquet. Decisión del usuario: resumen del motor solo chip
  de match 100% + confiabilidad de similares; vista dedicada tipo Dormidos.
- **Archivos**: `semaforo.py` (nuevo).

## (este commit) — 2026-08-10 · EQUIVALENTES entre marcas rivales (piloto)

### NUEVO: `equivalentes.py` — sustitutos entre marcas (Hikvision↔Dahua, etc.)
- **Qué** (usuario: "la competencia también vende marcas que compiten de
  frente; quiero el aviso 'TVC vende la domo Dahua equivalente más barata'"):
  capa SEPARADA del matching de identidad — (1) atributos duros por regex de
  ambos lados (tipo domo/bala/turret/ptz/nvr/dvr, MP, lente mm, tecnología
  ip/turbohd/hdcvi); (2) BLOQUEO mismo tipo+MP+tec, lente ±0.4mm, marca RIVAL
  (mapa explícito ampliable); (3) ranking por CERCANÍA DE PRECIO (los
  sustitutos reales se parecen hasta en precio — idea del usuario). Etiqueta
  SIEMPRE "EQUIVALENTE", jamás se mezcla con pares EXACTOS. Piloto cámaras:
  119 pares, 27 con la rival >10% más barata. Salidas:
  data/competencia/equivalentes.parquet + out/competencia_equivalentes.csv.
- **Precisión medida del matching general** (2026-08-10, juez = acuerdo de
  marca): EXACTO 88% ✓ · FUZZY_ALTO 60% · texto TF-IDF 4% · SBERT 2% —
  las capas de texto NO sirven para pares de precio (quedan como sugerencia
  de revisión); pendientes aprobar: candados marca+|gap|≤60% sobre EXACTOS.
- **CARRIL VECTOR (2026-08-10, decisión del usuario)**: el mapa de rivales NO
  acota — es solo el carril de alta confianza. Para cualquier otra marca, el
  carril VECTOR: mismo bloqueo de atributos + ranking por DESCRIPCIÓN
  VECTORIZADA (SBERT, solo dentro del bloque — su zona honesta) + cercanía de
  SUBTOTAL como doble firma; nivel EQUIVALENTE exige sim ≥0.55 Y |gap| ≤60%.
  Resultado: 118 → 447 pares (266 firmes por vector, p.ej. Uniview↔Hikvision
  sim 0.80 gap ±1%, Uniarch↔HiLook), 120 avisos de rival >10% más barata.
  Dependencias: sentence-transformers/torch (agregar a requirements como
  opcionales de matching).
- **Archivos**: `equivalentes.py` (nuevo).

## (este commit) — 2026-08-07 · Matching SYSCOM ↔ competidores (capas 3-5)

### NUEVO: cruce de modelos de la competencia con el catálogo SYSCOM
- **Qué**: sobre el registro por competidor, tres capas de matching:
  Capa 3 en `competencia.py match_syscom` (EXACTO tras normalizar /
  FUZZY_ALTO ≥0.75 / FUZZY_MEDIO ≥0.60, TF-IDF n-gramas + RapidFuzz);
  Capas 4-5 por TEXTO (TF-IDF + SBERT) restringidas a códigos ACTIVOS
  (`activos.py` → codigos_activos.parquet, venta en los últimos 3 meses);
  y `sys_lista.py` que une todo en la lista final de pares
  (distribuidor, modelo_competidor) → modelo_syscom con vía y score
  (prioridad: EXACTO > texto > fuzzy). Salidas:
  `data/competencia/syscom_vs_distribuidores.parquet` +
  `out/syscom_vs_distribuidores.csv`. `extract.py`: helper de apoyo (+77 líneas).
- **Archivos**: `competencia.py`, `activos.py` (nuevo), `sys_lista.py`
  (nuevo), `extract.py`.
- Nota: esto es el CIMIENTO de la comparativa de precios vs competencia,
  que sigue pendiente de decisión del usuario como regla del motor.

## 49491ba — 2026-08-07 · Canje de token de la API BI con error legible

### ✅ RESUELTO (2026-08-07 tarde) — credenciales restauradas, login completado
- El canje volvió a funcionar (token nuevo de 365 días emitido y guardado;
  `probar()` ✓ 1.4s). PERO al autenticar reapareció el problema RECURRENTE:
  `reporte_61` responde OK y `valor_inventario` regresa
  `500 acceso_bd_denegado` (el ping-pong de GRANTs de siempre — ver entrada
  bcc0d03). El pendiente estructural con TI sigue siendo ese.

### (histórico) AVISO A TI — credenciales de la API BI rechazadas en esta instalación
- **Qué pasa**: `POST /oauth/token` con `client_id=bi-colab-prod` regresa
  `401 {"error":"Credenciales inválidas"}` (verificado 2026-08-07). NO es la
  caída recurrente de Redshift (esa da `500 bi_no_disponible` en consultas,
  ya autenticado) ni tema de IP (eso regresa 307; el endpoint sí respondió).
  El secret en `.env.local` tiene forma plausible (47 caracteres) — o fue
  rotado/revocado del lado del servidor, o esta instalación necesita su
  propia alta de cliente.
- **Qué se necesita de TI/IT**: confirmar si el secret de `bi-colab-prod`
  sigue vigente y, si no, emitir uno nuevo (o un client nuevo para esta
  máquina). Con el secret correcto en `.env.local`, el alta se completa con
  `./.venv/bin/python api_bi.py probar` (canjea y guarda el token de 365
  días, y hace `SELECT ... FROM reporte_61 LIMIT 1`).

### 1. CÓDIGO: `token()` de `api_bi.py` reporta el detalle del error
- **Qué**: un 4xx al canjear el token moría en `raise_for_status()` sin
  contexto; ahora el error incluye status, `client_id` usado y el cuerpo de
  la respuesta del servidor (p.ej. `Credenciales inválidas`), que distingue
  un secret mal pegado de un cliente sin alta. El secret jamás se imprime.
- **Archivos**: `api_bi.py` (función `token()`).
- **Sin git**: reemplazar `api_bi.py` completo.

## 91a0838 + 3338a69 — 2026-08-07 · Precios de la COMPETENCIA (feed de correo)

### 0. NUEVO MÓDULO: `competencia.py` — registro por competidor + cambios
- **Qué** (usuario: "primero un registro por competidor, después identificar
  cambios, antes de la comparativa"): (1) `consolidar` → una BD parquet POR
  COMPETIDOR en `data/competencia/db/<fuente>.parquet` (historia modelo×fecha,
  idempotente) con **precios convertidos a USD** cuando la moneda es
  MXN/pesos (tipo de cambio de la SEMANA del dato, del panel; columnas
  `precio_venta_usd`/`precio_lista_usd` junto a las nativas); (2) `cambios` →
  detección por fechas consecutivas EN MONEDA NATIVA (el FX no es un cambio):
  PRECIO SUBIÓ/BAJÓ ≥1%, ALTA, STOCKOUT, REABASTECIDO →
  `data/competencia/cambios.parquet` + `out/competencia_cambios.csv` (7 días).
  Corre a diario en la cadena de 8:30 tras el extractor. La comparativa con
  nuestros modelos queda EXPRESAMENTE pendiente de decisión.
- **Archivos**: `competencia.py` (nuevo), `seguimiento_frenos.py` (cadena).

### 1. NUEVO MÓDULO: `extract_competencia.py`
- **Qué**: extractor IMAP solo-lectura del feed diario de correo
  ("CSVs de distribuidores" de saadclaw7@gmail.com): baja los CSV adjuntos de
  11 distribuidores (tvc, tecnosinergia, ct, exel, adises, cva, portenntum,
  absa, alcione, luguer, fibremex, dextra) a `data/competencia/` (~58K
  filas/día, esquema unificado: modelo, marca, precio_lista, descuento_pct,
  precio_venta, moneda, existencia, url). Idempotente; histórico desde
  2026-07-21. Agregado al cron diario de 8:30 (recolección solamente —
  NINGUNA regla del motor cambia todavía).
- **Credenciales**: `GMAIL_USER` + `GMAIL_APP_PASSWORD` (contraseña de
  aplicación de Google) en `.env.local` — jamás en código.
- **Archivos**: `extract_competencia.py` (nuevo), `seguimiento_frenos.py`
  (paso en la cadena diaria), `.env.example`.
- **Primer cruce medido** (2026-08-07): 842 modelos del motor con precio de
  competencia por match exacto de nombre; gap mediano de nuestro NETO vs el
  mejor competidor: +1.6% (46% más baratos). De los 339 SUBIR cruzados, tras
  el +4% el 38% seguiría ≤ competencia y el 31% quedaría >10% arriba.

## bcc0d03 — 2026-08-05 · Query de prueba de la API de BI con FROM obligatorio

### 1. CÓDIGO: `probar()` de `api_bi.py` ahora lee `reporte_61`
- **Qué**: el servidor de la API de BI cambió de regla (~2026-08): toda
  consulta debe leer al menos una tabla de la allow-list; un `SELECT 1` sin
  `FROM` regresa `400 sql_no_permitido`. La query de prueba de conexión pasó
  de `SELECT 1 AS ok LIMIT 1` a `SELECT 1 AS ok FROM reporte_61 LIMIT 1`.
- **Archivos**: `api_bi.py` (función `probar()`).
- **Sin git**: reemplazar `api_bi.py` completo.

### ⚠ RECURRENTE — el backend Redshift de la API pierde acceso repetidamente
- Detectado 2026-08-05 (mañana): `reporte_61` y `valor_inventario` (backend
  Redshift) con `500 bi_no_disponible`, `causa: acceso_bd_denegado`; la tabla
  MySQL respondía bien. Restaurado ese mismo día y verificado ✓… y CAÍDO DE
  NUEVO horas después (2026-08-05, verificado 3 intentos consecutivos con la
  misma causa). Historial: mismo patrón el 08-01→08-02 y el 07-28→07-31.
  **El acceso del service account de la API a Redshift no persiste** — cada
  restauración dura horas/días. Se necesita el arreglo de fondo del lado del
  servidor, no otra restauración manual. Mientras tanto el motor opera por
  Aurora/VPN (fallback automático).

## 8dbc29d — 2026-08-04 · Aplicador ERP, tope acumulado, reloj afinado

### 1. REGLA NUEVA: tope acumulado +10pts en 12 meses (con re-ancla por costo)
- **Qué**: un modelo puede subir máx +10pts ACUMULADOS en ventana móvil de 12
  meses, medidos contra el precio previo al primer cambio APLICADO del motor;
  el acumulado se REINICIA si el costo de reposición cambió ≥5% después de ese
  cambio y el costo del stock lo siguió (hubo compra). Recorta el paso al
  remanente o convierte a MANTENER. Exentos: frenos; la defensa de margen
  puede romper el tope. Inactivo hasta que el registro de aplicados tenga filas.
- **Archivos**: `escenarios.py` (bloque 🪜 tras `recomendar()`, antes de la
  mezcla de canal).
- **Sin git**: reemplazar `escenarios.py` completo.

### 2. REGLA AFINADA: reloj de muerte efectiva de dormidos (2 capas + recepción)
- **Qué**: una semana de silencio cuenta como "muerta" solo si
  (a) `disp_venta > 0`, (b) el disponible cubre ≥1 MES de su venta de época
  viva (`max(1, u_sem_era×4.345)`), y (c) ese stock suficiente ya estaba
  desde la semana ANTERIOR (la semana que recibe la reposición no cuenta —
  "si llegó el 30 de junio, junio no se toma"). El timer de re-decisión de
  recortes usa el mismo estándar. Evita clasificar como dormido lo que fue
  tema de disponibilidad. Impacto: vista de dormidos 2,057 → 758 modelos.
- **Archivos**: `dormidos.py` (cálculo de `sem_muertas` y `sem_corte`).
- **Sin git**: reemplazar `dormidos.py` completo.

### 3. NUEVO MÓDULO: `aplicar.py` — aplicación vía endpoint auditado del ERP
- **Qué**: aplica cambios de precio con `POST /api/agent/cambiar-precios`.
  Regla P1/P3: se reemplaza el precio PUBLICADO — solo P1 ⇒ `precio_nuevo1`;
  P1 y P3 ⇒ `precio_nuevo3` validando P3 < P1 (si no, rechaza con
  `REQUIERE_P1`). Dry-run por default; valida contra la corrida vigente
  (precio=sugerido ±0.5%, corte <21 días, paso ≤4.5%); `remate:"no"`; máx 200
  por corrida; log en `out/aplicaciones/`; al éxito registra en
  `monitoreo.aceptar` (medición + tope acumulado). Modo `servir` = puente
  local (puerto 8765) para los botones del reporte — la API key vive en
  `.env.local` (`ERP_API_URL`, `ERP_API_KEY`, `ERP_ACTOR_EMAIL`), jamás en HTML.
- **Archivos**: `aplicar.py` (nuevo), `.env.example` (variables ERP_*).

### 4. REPORTE: botones de aplicación + export CSV etiquetado
- **Qué**: casillas por fila + botón "🚀 Aplicar (N)" en el resumen; botón
  "Aplicar $X" del panel detallado cableado al puente; botón "⬇ CSV" exporta
  la selección filtrada con columnas `modelo, precio_sugerido, comentario`
  donde comentario = `Motor de Precios v3 | ciclo <corte> | <±X%>` (la
  etiqueta para el campo comentario del ERP — identifica cambios del motor).
  El monto $$ de Dormidos sigue a la selección/filtro activo. Terminología:
  "modelo" (no SKU) en todos los textos visibles.
- **Archivos**: `reporte_top.py`, `panel_sku.py`.

### 5. Otros de la misma entrega
- `escenarios.py`: mezcla de canal registra `canal_ajuste` (explicación en el
  detallado, sin chip en resumen); avisos de frescura de censo/anclas.
- Paneles embebidos de "costo movió" acotados al top-100 por utilidad (el
  caché nocturno `out/paneles/` cubre el resto).
- `api_bi.py`/`seguimiento_frenos.py`: la cadena diaria sobrevive errores
  `SystemExit` (bug que mató la corrida del 2026-08-03).
- Docs: `ONBOARDING.md` (guía de entrega), ARQUITECTURA al día.

---

*(Las entradas nuevas se agregan ARRIBA de esta línea con su hash de commit.)*
