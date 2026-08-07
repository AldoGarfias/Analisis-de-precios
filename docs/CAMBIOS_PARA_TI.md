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
