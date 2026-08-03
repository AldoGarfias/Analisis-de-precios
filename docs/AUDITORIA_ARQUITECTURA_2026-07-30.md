# Auditoría integral de arquitectura — 2026-07-30 (previa a migración)

Cuatro auditorías independientes y paralelas: (1) conflictos de reglas,
(2) secuencia y dependencias, (3) doc vs código, (4) umbrales y constantes.
Este documento es el registro completo; el plan de corrección vive al final.

## VEREDICTO

La arquitectura es sólida en su núcleo — 60+ constantes y reglas verificadas
exactas entre doc y código, el árbol de decisión respeta el piso de margen, el
cierre de ciclo es inmune a re-corridas, los archivos inmutables funcionan, y
los candados auditados previamente (regex EN MEDICIÓN, freno×abstención,
dormidos∩motor=∅) operan bien. PERO hay 7 hallazgos críticos que deben
corregirse ANTES de aceptar cambios reales o migrar, porque producen medición
incorrecta o acción equivocada. Patrón común: los módulos satélite (monitoreo,
frenos, dormidos, checkpoint) se comunican con el árbol por textos de motivo y
CSVs con semánticas distintas (precio observado vs administrado; venta 4s vs
26s vs época viva; caída cruda vs ajustada por mercado), sin sello de corte.

## CRÍTICOS (corregir antes de aceptar cambios / migrar)

C1. **Censura de monitoreo compara contra el precio del panel (semanal, de
    ventas observadas), no contra la lista administrada.** Con el cron diario,
    toda aceptación ≥1% se censura en días ("lista cambió" falso) ⇒ el candado
    EN MEDICIÓN se desactiva y el precio puede re-tocarse de inmediato.
    [monitoreo.py:175-191 vs panel.py:154-165]. FIX: censurar contra la lista
    administrada fresca (vigía diaria trae precio_1/3 cada día).

C2. **El motor puede "revertir" sus propios frenos.** señales_revision no
    distingue subidas del propio motor: un freno que funcionó (venta cae) se
    lee como "aumento dañino" ⇒ BAJAR prioritario exento del lock, mientras
    seguimiento_frenos dice sostener hasta reabasto (cobertura <6 sem).
    [escenarios.py:336-366,400-403 vs seguimiento_frenos.py:41,230-239].
    FIX: excluir de cayo_tras_aumento los SKUs con freno activo en el registro.

C3. **Sin sello de corte: el reporte publica hoy con panel 07-24, recos 07-29 y
    dormidos 07-27 mezclados; nada lo detecta.** [reporte_top.py:36,
    panel_sku.py:93-96]. FIX: columna corte/generado_en en recomendaciones.csv
    y assert de coherencia en cada consumidor (reporte, ciclo.emitir,
    monitoreo.aceptar, dormidos).

C4. **Sorteo de holdout inestable dentro del ciclo**: la semilla es fija pero el
    conjunto elegible cambia a diario (el lock EN MEDICIÓN usa Timestamp.today y
    el registro vivo) ⇒ re-correr escenarios a mitad de ciclo puede producir
    holdout H2≠H1; monitoreo.aceptar usa el vivo y el snapshot del ciclo el
    original ⇒ contaminación del control. [escenarios.py:632-651,769-779 vs
    ciclo.py:47, monitoreo.py:93-94]. FIX: aceptar --ciclo debe leer el holdout
    DEL SNAPSHOT del ciclo, no de las recos vivas.

C5. **dormidos "PAUSA POR FALTA DE STOCK" sugiere precio de época viva SIN piso
    de margen** (única bajada del sistema que puede quedar bajo costo+3pts; la
    regla es innegociable). [dormidos.py:303-304 vs 347-351]. FIX: aplicar
    lista_min como en REACTIVAR.

C6. **Checkpoint semanal sin chequeo de frescura de ventas** ⇒ semana parcial =
    falsas "DEMANDA BAJO p10" en cache inmutable que heredan cierre y
    autopsias. [checkpoint_semanal.py:52-68]. FIX: abortar (o marcar) si
    ventas.max() < fin de la semana a evaluar; extraer antes en el cron del
    lunes.

C7. **Los dos primeros pasos del cron sin try/except** ⇒ un tropiezo en frenos
    mata monitoreo, vigía y checkpoint de ese día, en silencio.
    [seguimiento_frenos.py:375-376]. FIX: try/except por paso + resumen de
    fallos al final.

## MEDIOS (corregir pronto; no bloquean pero ensucian)

M1. Re-toque dinámico corto (2 ciclos) censura por diseño los veredictos de 8 y
    12 semanas — el docstring de monitoreo dice lo contrario. Decidir: o el
    re-toque mínimo respeta el horizonte definitivo (12 sem) para una fracción
    de SKUs, o se acepta perder 8/12 en re-toque corto y se documenta.
M2. Sobrestock exento del lock EN MEDICIÓN pero NO del holdout (urgencias
    opuestas para el mismo motivo). Decidir y documentar.
M3. Registro de frenos dedup por código ignora tipo/estado: un freno nuevo de
    un SKU con historia vieja no se da de alta nunca. [seguimiento_frenos.py:82,112]
M4. REVERTIR: umbral seguimiento 20% crudo vs motor 25% ajustado por mercado ⇒
    alertas contradictorias el mismo día. Unificar al ajustado.
M5. Baselines de costo: checkpoint usa el PRIMER snapshot de la vigía (no el de
    emisión del ciclo); defensa de margen dice "emisión" en comentario pero usa
    el último snapshot semanal. Anclar ambos a la fecha de emisión.
M6. Escenarios decide sobrestock con el último snapshot semanal sin cota de
    antigüedad (frenos sí etiqueta datos viejos). Añadir aviso/cota.
M7. Frescura de señales web y AWS jamás verificada al inyectar (parquet viejo =
    chips presentados como vigentes). Añadir aviso de antigüedad.
M8. Circularidad senales_web↔escenarios: benigna en régimen; bootstrap desde
    cero requiere orden run→señales→re-run escenarios (documentar en run.py);
    SKUs nuevos quedan sin señal una corrida.
M9. vigia_cambios.csv se sobrescribe a diario (el diario de alertas se pierde;
    los snap_* permiten recomputar). Pasarlo a archivo por fecha o append.
M10. Monitoreo censura con panel viejo también para detectar reverts reales
    (además de C1): la lista fresca de la vigía resuelve ambos.
M11. Checkpoint mide venta CON proyectos contra bandas SIN proyectos: PICO>p90
    es útil así (detecta proyecto) pero BAJO p10 puede quedar enmascarada.
    Documentar o filtrar como el panel.
M12. Dos ciclos abiertos posibles: checkpoint vigila el más reciente,
    ciclo.cerrar toma el más antiguo. Impedir doble ciclo abierto en emitir.

## CONSTANTES / DISPLAY

K1. 4.345 vs 4.33 semanas/mes conviven (incluso dentro de escenarios.py:201 vs
    684). Unificar en constante compartida (definir la canónica: 4.345).
K2. Elasticidades −0.83/−0.95/−1.05 hardcodeadas en tooltips (reporte_top:91,
    panel_sku:348); el modelo las re-estima cada corrida. Inyectar dinámicas.
K3. Badge demanda: resumen ±5%/mes vs detallado ±3%/mes. Unificar (5%).
K4. Gate de señales web: detallado muestra etiquetas con ≥20 vistas, resumen
    exige ≥100. Unificar (≥100 para etiquetar; <100 solo números).
K5. Cubetas JS: cambios <3.5% caen todos en "2-3%" aunque la calibración
    excluye <1.5% — para pasos de ±1% se muestra tasa base no medida. Gate.
K6. UMBRAL_PRECIO significa 0.05 en dormidos y 0.001 en vigía (mismo nombre,
    conceptos distintos). Renombrar uno.
K7. SBA duplicada no idéntica (forecast recorta ceros iniciales,
    intermitentes.py no). Unificar si se re-corre el examen.
K8. Escalera de umbrales de "cambio de lista" (0.001/0.005/0.01/0.015/0.02/
    0.05) sin mapa — documentar tabla en ARQUITECTURA.
K9. se=0.13 mágico en reporte_top:229 sin fuente. Documentar o derivar.

## DOC (ARQUITECTURA_V3.md y CLAUDE.md)

D1. §4 dice Z90=1.645 y GRID sin ±3%; el código usa Z95=1.96 y ±3. Actualizar.
D2. §6/§3 traen calibración PRE-blindaje (24,358 eventos, 50%/+4.2%); la
    vigente es 8,554 limpios (59%/+11.9%). Actualizar y marcar la vieja.
D3. §1 dice eventos ≥2%; código y §3/§6: ≥1.5% (también docstring de
    valida_decisiones dice 2% — corregir ambos).
D4. §4 "árbol completo" omite KVI (regla 5 real) y el candado de re-toque.
D5. Notas de auditoría 1/3/4 describen defectos ya corregidos — marcarlas.
D6. Invariante §7.10 "solo existe registro de frenos" — ya existe monitoreo.
D7. CLAUDE.md: guardrail ±10pts NO implementado (nadie lo hace cumplir —
    decidir: implementarlo como tope acumulado o quitarlo del doc); pendientes
    ya hechos (DML, escalera EB, SBA); prioridad #4 sugiere marca/línea en el
    GBM (contradice regla; el código correctamente no las usa).
D8. forecast.py docstring dice "pérdida Poisson"; usa squared_error sobre ratio.
D9. run.py docstring omite forecast.py (PASOS sí lo trae).

## VERIFICADOS SIN PROBLEMA (muestra)

- Piso de margen inviolable en el motor principal (se evalúa tras todas las
  bajadas); KVI solo bloquea SUBIR; freno exento de KVI.
- Regex EN MEDICIÓN matchea exactamente los 3 motivos reales; abstención de
  freno NO contiene "frenar" (candado 07-27 operando).
- dormidos ∩ motor = ∅ por construcción.
- ciclo.emitir congela copia; cerrar lee snapshot+panel con chequeo de
  frescura del panel (el único explícito del pipeline).
- Orden del cron correcto (vigía antes de checkpoint los lunes).
- run.py aborta en cascada; guard anti-snapshot-parcial en 3 consumidores.
- Z95, umbral costo 2%, piso 3%, paso 4%, sobrestock 12m, territorio, señales
  web ½×/2×, cubetas 3.5/5.5/9: consistentes entre copias.
- Tasa base del encabezado, ERR_MED, ε por fila, mediana web: dinámicos.
- Parseo de '%' del ERP en un solo lugar (panel.py) — sin cruces de unidades.

## Estado de correcciones

2026-07-30 (aprobado por el usuario) — CRÍTICOS APLICADOS Y VERIFICADOS:
- [x] C1: censura contra lista ADMINISTRADA (vigía diaria, precio_1/3 según
      tipo_precio, guardado en el registro al aceptar); sin snapshot ⇒ censura
      pospuesta con aviso (nunca contra el panel). [monitoreo.py]
- [x] C2: SKUs con freno activo excluidos de cayo_tras_aumento y no_rota —
      230 excluidos en la corrida vigente; su re-decisión es del seguimiento
      diario. [escenarios.py señales_revision]
- [x] C3: sello de corte (columnas corte/generado_en en recomendaciones) +
      assert en cargar_ctx (cubre reporte y paneles) y en ciclo.emitir.
- [x] C4: monitoreo.aceptar --ciclo lee del SNAPSHOT del ciclo abierto, no de
      las recos vivas; COLS_SNAP ahora incluye n_clientes/tipo_precio/corte.
- [x] C5: piso costo+3pts aplicado también en PAUSA POR FALTA DE STOCK
      (dormidos), igual que REACTIVAR.
- [x] C6: checkpoint recorta a semanas CUBIERTAS por la extracción y avisa si
      está atrasada (no más falsas "BAJO p10" en cache inmutable).
- [x] C7: cadena del cron con try/except por paso + resumen de fallos +
      notificación "REVISAR" si algo falló.
- [x] M5: línea base de "COSTO MOVIÓ" del checkpoint = snapshot más cercano a
      la emisión del ciclo. M12: emitir rechaza segundo ciclo abierto.

Regenerado y verificado: escenarios (mismas 12,594 decisiones — los frenos
excluidos nunca se aplicaron, así que C2 no cambia números HOY, protege el
futuro), dormidos, reporte (sintaxis JS OK, publicado).

Pendientes M1-M11 (menos M5/M12) y K/D: por aplicar en tanda siguiente; las
3 preguntas de regla (±10pts, sobrestock vs holdout, re-toque vs horizonte 12)
esperan decisión del usuario.

---

# RONDA 2 (2026-07-31, tras migración Redshift / ventana 2024 / meses v3 /
# forecast mensual / capa SKU / vigía / checkpoint / cache)

Cuatro auditores re-verificaron TODO. Veredicto: C1-C7 y M5/M12 AGUANTAN
(verificados en código por dos auditores independientes); el código nuevo y
las capas nuevas del doc son notablemente exactos; los hallazgos nuevos se
concentraron en lo construido hoy.

## CRÍTICOS NUEVOS — CORREGIDOS EN ESTA TANDA
- [x] N1: defensa de margen con base MÓVIL (ex.semana.max()) era ciega a
      subidas graduales de costo — base anclada a la semana de EMISIÓN del
      ciclo abierto (como el checkpoint). [seguimiento_frenos.revisar_costos]
- [x] N2: revision_costos.csv sin expiración acumulaba pct contra costo_base
      de otra época y descontaba el margen DOS veces — eventos >21 días sin
      movimiento expiran (el movimiento nuevo abre evento fresco); purga a
      45 días. [vigia_diaria._registrar_revision_costos]
- [x] N3: forecast_mensual examen/generar sin chequeo de frescura del panel
      (el análogo de C6) sellaban un mes PARCIAL en archivos inmutables y
      meses_stock heredaba demanda subestimada — guard _panel_cubre() en
      ambos (pospone al día siguiente del cron).
- [x] N3-flujo: la cadena del cron corría monitoreo ANTES que la vigía (la
      censura usaba el snapshot de AYER) — reordenada: vigía primero.
- [x] N9-flujo: la voz "motor u0" del examen usaba la corrida vigente
      (lookahead) — ahora se ARCHIVA ex-ante en el pred_* mensual (columna
      u0_mensual) y el examen la lee de ahí.
- [x] Juez nuevo CODIFICADO en _veredicto(): media geométrica de ratios +
      mediana (<1 ambas) + winsorización de meses anómalos declarados
      (2026-06 aniversario) — reemplaza la vara "5 de 6" en backtest y
      backtest-ml.

## MEDIOS — CORREGIDOS
- [x] N4: guard de idempotencia en analisis_eps_sku.aplicar (aborta si la
      capa ya está aplicada).
- [x] N5: C2 excluía frenos para SIEMPRE (estado != cerrado) — ahora solo
      frenos ACTIVOS (esperando_reposicion); un reabastecido re-decidido
      vuelve a las señales del motor.
- [x] N7: meses_stock anclado al corte del PANEL, no al reloj.
- [x] N8: vigía agregaba cantidad_bo con SUM (inflado ×almacenes) — MAX en
      ambas rutas (API y Aurora), como el resto del sistema.
- [x] N9: sello de corte extendido a dormidos.py y monitoreo.aceptar (ruta
      manual) — C3 ahora cubre TODOS los consumidores de su spec original.
- [x] N12: motCorto etiquetaba "FRENO" la abstención-freno → "ABSTENCIÓN".
- [x] N13: el detallado ahora dice "afinado con SUS propios cambios de
      precio" cuando aplica la capa SKU.
- [x] Docstrings/textos mentirosos corregidos: meses_stock (v3), módulo
      forecast_mensual (ensamble adoptado), valida_decisiones (≥1.5%,
      ventana), run.py (6 pasos), monitoreo (claim de auto-censura),
      notificación AWS (ya no alimenta meses de stock), header de la columna
      Stock (v3), tooltip sin el "6/6" congelado.
- [x] DOC: §4 (Z95, GRID±3), calibración VIGENTE (11,567 eventos) en §1/§6,
      invariante 7.10, notas de auditoría marcadas históricas, fila "Rol" de
      EVALUACION (v3 adoptada), CLAUDE.md actualizado (Redshift titular,
      ventana 2024, meses v3, capa SKU, ±10pts marcado NO implementado).

## PENDIENTES (no bloquean; en orden de valor)
- N6 (deliberado, documentado): el forecast no rescata SKUs sin vía propia.
- N10: dedup del registro de frenos por código cruza tipos freno/dormido (M3
  agravado) — requiere rediseño de llaves del registro.
- N14: STOCKOUT del checkpoint usa existencia total (no vendible) de vigía.
- N15/M9: vigia_cambios.csv se sobrescribe; snapshots crecen sin límite.
- N16/D4/D9 parciales: §4 aún sin la regla KVI/candado en su lista numerada;
  mermaid §2 sin extract_api; §2/§3 con conteos de la corrida 07-27.
- K3/K4/K5/K6/K7/K9 (display/nombres, de la ronda 1) y CORTE_TRAIN fijo.
- Decisiones de regla del usuario: ±10pts, sobrestock vs holdout, re-toque
  corto vs horizonte 12, cierre formal del ciclo de vida de frenos.
- Caveat ciclo 0: su snapshot es pre-C3/C4 (sin tipo_precio/n_clientes) —
  al aceptar, periodo_retoque usará clientes=0 (+1 ciclo, conservador).

---

# RONDA 3 (2026-08-01) — tras remate/clasificación, cadencia, reloj efectivo y ⚓ ancla

4 auditores paralelos (conflictos / secuencia / doc-vs-código / constantes).
~40 cifras verificadas EXACTAS (trinidad del ancla 1,016/$29K/$168K al peso,
TPAR5AC, 823/574→0, juez del forecast, N1-N13 de ronda 2 intactos).

## CORREGIDO EN ESTA RONDA (todo aplicado, regenerado y publicado)

- [x] A1/R3-1 (ALTO): el SELECT del censo vía API no traía remate/clasificación
      (replace anterior no-opeó) → columnas agregadas; censo con `fecha_censo`;
      dueño en el cron (lunes, con checkpoint); escenarios AVISA si falta el
      censo, si es formato viejo o si tiene >7 días. (El API sigue 500 en ese
      GROUP BY — plan B Aurora operando; el fix queda listo.)
- [x] A2/H8 (ALTO): PUERTA DE LA SEMANA 8 en la cadencia — con <8 sem muertas
      NO se emite recorte (956 en sala de espera con texto explícito; antes el
      CSV traía 765+ recortes fuera de regla). Vista ≥8 (inclusiva): +184 SKUs
      en la frontera que eran invisibles.
- [x] Reloj con lookahead (+1 sem de existencias vs panel): recortado al corte
      del panel; ya NADIE entra a la vista por la semana fantasma.
- [x] M1: utilidad de compañeros fuera del motor — fallback del panel (26 sem)
      en vez de $0. VEREDICTO HONESTO: el 97% de esos compañeros son fletes/
      kits/servicios (ENVIOGRATIS, /K…) sin precio administrado — el $0 era
      aproximadamente correcto; el fallback cubre la franja legítima (bloqueo
      1,016→1,018, arrastre casi igual). El hallazgo era real pero menor.
- [x] M2: nota de política en el panel detallado — remate/ancla aclaran que
      las ganancias de la tabla de escenarios son teóricas/no disponibles.
- [x] M4: juez del forecast winsoriza al SEGUNDO peor ratio (.iloc[-1]), como
      declara — juez codificado = juez declarado.
- [x] M5: el timer de re-decisión del recorte corre en semanas CON stock.
- [x] M6: tipo de lista (1/3) = el de la ÉPOCA VIVA, no el modal histórico.
- [x] M7/B2: piso costo+3pts en TODAS las ramas de cadencia (sostener,
      REVERTIR acotado al piso con texto del precio real, REABASTECIDO).
- [x] H2/B3: fronteras de bucket del ancla alineadas con el pd.cut del estudio
      (> en vez de ≥: attach exacto 0.20/0.40 va al bucket inferior).
- [x] H3: filtro duro persistido — `analisis_canasta.py estricto`.
- [x] H14: chip REMATE NaN-safe (_clasif_remate; 0 "REMATE nan" en el HTML).
- [x] K1 cerrado: 4.33→4.345 en dormidos (×2), modelo, seguimiento_frenos.
- [x] Cifras vivas: evidencia global (45/49) y reabastos (23,415/82/72) se
      leen del parquet en cada corrida; tooltip del ancla fechado.
- [x] R3-2..R3-9: censo al cron; mapa de anclas con sello `corte` + aviso de
      staleness en escenarios; error amigable sin ventas_*.parquet; docstring
      run.py (7 pasos); try separados examen/generar + falta-AWS a fallos;
      celda de dormidos muestra el reloj efectivo ("sem muerto c/stock");
      COLS_SNAP += remate/clasif_erp; aviso B5 al no anclar base de costos;
      contador de MI/MC excluidos en dormidos.
- [x] DOC: ANALISIS_VENTA_CRUZADA "regla" alineada al código (50%, sin
      ×attach, solo bloqueo, punto 4 no implementado); ARQUITECTURA §2/§3/§6
      refrescados a la corrida vigente (Δ +$90,986 declarado; Δ intermedios
      marcados SUPERADOS; ε vivas en parquets); CLAUDE.md pipeline 7 pasos sin
      ε congeladas; docstrings dormidos/escenarios/reactivación/reporte_top.
- [x] Regenerado: anclas → escenarios (×2) → dormidos → reporte; JS OK;
      publicado. Corrida vigente: 5,650 SUBIR / 3,170 BAJAR / 4,053 MANTENER,
      Δ +$90,977/sem, vista dormidos 2,070.

## PENDIENTES NUEVOS DE RONDA 3
- REVERTIR de cadencia hoy = 0 casos (el timer con-stock reinició relojes);
  reaparecerán conforme los recortes acumulen 4 semanas CON stock.
- El API de BI sigue 500 en el GROUP BY del censo (pendiente de sistemas).
- Paneles cacheados (out/paneles/) se refrescan con el cron de las 0:00 —
  hasta entonces no traen la nota M2.
- Sigue abierto (rondas previas): N10, N14, N15, K3-K9, CORTE_TRAIN fijo,
  decisiones de regla del usuario (±10pts acumulado, sobrestock vs holdout,
  re-toque corto, cierre de frenos, valuación de canibalización, presupuesto
  de cambios).
