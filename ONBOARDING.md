# Motor de Precio Óptimo v3 — Guía de entrega (equipo ERP)

Motor de pricing B2B (SYSCOM). Sugiere SUBIR/BAJAR/MANTENER el **precio de
LISTA** por SKU con elasticidad medida, guardrails y seguimiento de cada
decisión. **Fase de pruebas: jamás escribe precios a la BD** — todo sale a
archivos locales (`out/`); los cambios reales se aplican solo por el flujo
auditado del ERP.

Documento autoritativo de reglas: **`docs/ARQUITECTURA_V3.md`** (se deriva del
código y se audita contra él — 4 rondas de auditoría en
`docs/AUDITORIA_ARQUITECTURA_2026-07-30.md`). Convenciones y decisiones de
diseño: `CLAUDE.md`. Esta guía es el mapa de entrada.

---

## 1. BASES DE DATOS (nombres exactos y orígenes)

| Fuente | Vía | Tablas | Rol |
|---|---|---|---|
| **Redshift** (espejo del ERP en AWS) | API de BI `https://developers.syscom.mx` — OAuth2 client_credentials, `POST /api/v1/bi/query` | `reporte_61`, `valor_inventario`, `v_bi_eventos_interacciones` | **TITULAR** (sin VPN; requiere IP en allow-list — HTTP 307 = IP no autorizada) |
| **Aurora MySQL 5.7** (réplica de lectura del ERP) | VPN + pymysql (`db.py`), usuario solo-SELECT | `reportes.reporte_61`, `reportes.valor_inventario`, `2015epcom.cat_almacen` | **SUPLENTE** (fallback automático en todos los extractores) |

- **`reporte_61`** (~72M renglones, 2016→hoy; ventana operativa 2024→hoy):
  renglón de documento. Campos usados: `fecha, folio, codigo, codigo_cliente,
  cantidad, precio, tipo_precio, descuento_uno, descuento, desc_fin, subtotal,
  costo_venta, tipo_cambio, concepto, clasificacion_descuento, via, kit,
  estatus`. SKU = `codigo` (NO usar `marca`/`linea`). `estatus`:
  Activa (factura) / Cotizacion / Nota credito / Cancelada. Solo
  `tipo_precio ∈ {1,3}` (1=lista, 3=oferta); `precio=0` = componente de kit.
  Stack de descuentos verificado: `neto = precio·(1−d1)(1−d2)(1−desc_fin/100)`.
  Moneda: USD (tipo_cambio por renglón para MXN).
- **`valor_inventario`** (snapshot diario por código×almacén, historia desde
  2018): `existencia, existencia_total, precio_1, precio_3, cantidad_bo,
  costo_prov, costo_total_dolares, proveedor, remate, clasificacion`.
- **`cat_almacen`** (`vendible=1` = almacenes de venta) — SOLO vive en Aurora;
  cacheado en `data/cat_almacen_vendible.json`.
- Reglas de migración (no negociables): NULL→'', limpiar U+200B/mojibake en
  `codigo`, **jamás filtrar por caracteres acentuados** (la API rompe la í:
  "l?nea").

Credenciales: SOLO en `.env.local` junto a `db.py` (plantilla `.env.example`;
variables: `DB_HOST_READ, DB_PORT, DB_USER, DB_PASSWORD, DB_DATABASE,
BI_CLIENT_ID, BI_CLIENT_SECRET, BI_TOKEN` — el token de BI dura 365 días y el
cliente lo reescribe en `.env.local`, que necesita permiso de escritura).

## 2. CAPAS Y ORDEN PRECISO

### Pipeline del ciclo (`run.py`, 8 pasos, aborta si uno falla)

1. `panel.py` — panel código×semana (higiene: tipo_precio∈{1,3}, sin kits,
   sin proyectos para demanda de precio) → `data/panel.parquet`
2. `modelo.py` — elasticidad ε PPML con ceros y stockouts excluidos →
   `elasticidad_sku.parquet`, `eps_por_sku.parquet` (se RE-ESTIMAN cada corrida)
3. `analisis_eps_sku.py aplicar` — capa SKU de ε (Empirical Bayes por eventos
   propios de lista, solo SKUs con ≥3 eventos)
4. `validar.py` — backtest out-of-time → bandas de error
5. `forecast.py` — u0 (demanda base) GBM residual, campeón validado
6. `analisis_canasta.py anclas` — mapa de venta cruzada (folios 26 sem)
7. `analisis_canal.py mezcla` — % venta en línea por SKU
8. `escenarios.py` — árbol de decisión → `out/recomendaciones.csv` + `out/escenarios.csv`

### Árbol de decisión dentro de `escenarios.py` (orden exacto)

**Pre**: exclusión MI/MC (muestras invendibles) + flags remate/clasificación.
**`recomendar()`**: dirección por utilidad (u0·f^ε) → revertir aumento dañino
→ sobrestock ≥12 meses manda → no-rota → frenar por reabasto → KVI protege
imagen → piso de margen costo+3pts → abstención a baja confianza.
**Overrides post (la última palabra, en orden)**:
0. 🌐 Mezcla de canal — solo CONFIANZA (media↔alta) en SUBIR ≤4pts según %línea
1. 🏷️ REMATE MANDA — remate S / R0-R4: dejar agotar (SUBIR/BAJAR→MANTENER)
2. ⚓ ANCLA DE CANASTA — bloquea SUBIR si arrastre a compañeros ≥50% de la ganancia (frenos exentos)
3. 🧪 RE-TOQUE — medición abierta no se re-toca hasta su fecha (exentos: frenar/revertir/sobrestock)
4. AWS + señales web — informativas (columnas, jamás dirección)
5. 🎲 HOLDOUT 15% — grupo de control estratificado, semilla = corte
6. Sello de corte + guardado local

**Guardrails**: paso máx ±4pts por ciclo de 3 SEMANAS (un precio nunca se
mueve dos semanas seguidas); tope ACUMULADO +10pts en ventana móvil de 12
meses (ancla = precio antes del primer cambio aplicado; re-ancla cuando el
costo real cambia ≥5% con compra); piso costo+3pts; sobrestock ≥12m no sube;
abstención a baja confianza (preferir no opinar a opinar mal). Todo cambio
aplicado en el ERP lleva la etiqueta `Motor de Precios v3 | ciclo <corte> |
<±X%>` en el campo comentario (la trae el CSV del botón ⬇ del reporte) —
esa etiqueta es la que alimenta el registro de aplicados y el tope acumulado.

### Segunda capa: `dormidos.py` (SKUs sin venta reciente, fuera del motor)

Reloj de MUERTE EFECTIVA (solo semanas sin venta CON stock). Cadencia desde la
semana 8: escalera de recorte 8/16/26/52 sem → −5/−10/−15/−25% vs lista
pre-silencio (remate dormido +10pts, tope 35%), re-decisión cada 4 semanas con
stock (REVERTIR si no revivió y su grupo proveedor no respalda), piso
costo-del-stock+3pts siempre. Evidencia: estudios de reactivación
(`analisis_reactivacion.py` → parquets A/B, cifras vivas).

### Etapa 2 (en construcción, NO toca al motor)

Win-rate de cotizaciones (`extract_cotizaciones.py`, `winrate.py`,
`modelo_winrate.py`): P(ganar | precio ofrecido, contexto) — política de
descuento del vendedor. Estudios: `docs/ANALISIS_CANAL_LINEA.md`,
`docs/ANALISIS_VENTA_CRUZADA.md`.

## 3. ACTUALIZACIÓN Y SEGUIMIENTO (requisito: diario)

### Cron diario 8:30 L-V — `seguimiento_frenos.py` (cada paso con red; fallos → notificación)

1. Frenos por reabasto (re-decidir al llegar stock)
2. Defensa de margen por costo (base anclada a la EMISIÓN del ciclo)
3. **Vigía diaria** (`vigia_diaria.py`): snapshot de `valor_inventario` para
   los códigos del motor → detecta costo ±2%, listas >0.1%, stockouts, BO →
   `out/vigia_cambios.csv` + `out/revision_costos.csv` (dispara "revisión por
   costo", expira 21 días)
4. Monitoreo de decisiones aplicadas (checkpoints 7d, censura contra lista
   administrada, veredictos 4/8/12 sem, bandera 🚩 descuento-amortiguador)
5. Vigilante del API de BI
6. [lunes] checkpoint semanal del ciclo + censo remate/clasificación + mezcla
   de canal + reporte de fuga de descuentos
7. [día ≤10] examen mensual de forecasts + archivo EX-ANTE del mes nuevo

### Cron nocturno 0:00 — `genera_paneles.py`

Cache de ~20,000 paneles HTML detallados por SKU (`out/paneles/` + índice con
buscador) — el requisito de "detallado rápido". Regenerar tras cada corrida.

### Manual (por diseño, con dueño)

- Extracción de ventas/existencias (`extract_api.py`) — recomendado agregarla
  al cron diario en el nuevo entorno.
- `run.py` + `dormidos.py` + `reporte_top.py` por ciclo (cada 3 semanas).
- `ciclo.py emitir/cerrar` + `monitoreo.py aceptar --ciclo` (decisiones).
- `seguimiento_frenos.py registrar` / `registrar-dormidos` tras cada corrida.
- Export AWS Forecast la 1ª semana del mes: `aws_forecast.py <csv>`.
- APLICAR precios: `aplicar.py <csv del botón ⬇> --aplicar` — POST al endpoint
  auditado del ERP (/api/agent/cambiar-precios), SOLO el precio que cambia
  (precio_nuevo1 o 3 según tipo_precio), dry-run por default, valida contra la
  corrida vigente, y al éxito registra en monitoreo (medición + tope acumulado).
  Credenciales: ERP_API_URL/ERP_API_KEY/ERP_ACTOR_EMAIL en .env.local.

### Cachés y estado (qué archivo escribe quién) — tabla completa en la ronda 4 del ledger de auditoría

**ESTADO IRREEMPLAZABLE al migrar** (no existe en ninguna BD, copiar siempre):
`out/seguimiento_frenos.csv`, `out/monitoreo_cambios.csv`, `out/ciclos/`,
`out/checkpoints/`, `data/forecast_mensual_propio/pred_*.parquet` y
`data/aws_forecast/archivo/pred_*.parquet` (forecasts congelados EX-ANTE),
`data/vigia/snap_*.parquet`. Todo lo demás se regenera desde la BD.

## 4. ARRANQUE EN OTRO ENTORNO

1. Python 3.12 + `pip install -r requirements.txt`.
2. `.env.local` junto a `db.py` (ver §1) — probar con `python api_bi.py probar`.
3. Red: IP en allow-list del API (o VPN a Aurora como suplente).
4. Datos: copiar `data/reporte61/` (~550 MB, ahorra días) o extraer:
   `extract_api.py` (ventas 2024→hoy), `extract_api.py existencias`,
   `extract.py kits`, `extract.py proveedores`, `extract_api.py proveedores`.
5. `python run.py` → `python dormidos.py` → `python reporte_top.py`.
6. Crons (hora de México): `30 8 * * 1-5 seguimiento_frenos.py` y
   `0 0 * * * genera_paneles.py`. OJO portabilidad: las notificaciones usan
   `osascript` (macOS) — en Linux caen a logs y archivos `out/ALERTA_*.txt`;
   en una laptop que duerme, cron pierde ejecuciones (usar servidor o launchd).
7. NO necesitan: `v1_cotizaciones/`, `v2_elasticidad/` (referencia histórica),
   scripts ad-hoc (`valida_espejo.py`, `chronos_examen.py`, `dml.py`, etc.).

## 5. SEGURIDAD (innegociable)

- Credenciales solo en `.env.local` (en `.gitignore`); jamás en código/commits.
- `db.py query()` rechaza todo verbo que no sea SELECT/SHOW/DESCRIBE/EXPLAIN;
  el API solo acepta SELECT con LIMIT ≤1000, 30 req/min.
- **El pipeline nunca escribe precios reales al catálogo** — la escritura a BD
  está DORMIDA (`db.py`, tablas `precio_optimo_*` únicamente).
- `data/` y `out/` contienen datos de clientes y pricing: JAMÁS subirlos a git
  (cubiertos por `.gitignore`); compartir solo vía git, nunca la carpeta.
