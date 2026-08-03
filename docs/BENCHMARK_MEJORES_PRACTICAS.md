# Benchmark v3 vs mejores prácticas (investigación web exhaustiva, 2026-07-27)

> Síntesis de 5 investigaciones paralelas (~60 búsquedas, ~120 fuentes leídas):
> arquitectura de pricing B2B, econometría de elasticidad, forecasting,
> experimentación/medición, gobernanza/operación. Detalle y URLs en el artifact
> "Benchmark v3 — mejores prácticas" y en la conversación de origen.

## Veredicto

El núcleo metodológico del v3 **cumple o supera** la práctica publicada
(vendors y consultoras): PPML+FE para ceros, exclusión de stockouts, EB
jerárquico, roles KVI/SD/PG, guardrails de 5 familias, inventario manda,
human-in-the-loop explicable, regla FVA ("si no le gana a la media, no entra"),
registro de cambios + replay calibrado, lista separada de descuento.
Las brechas NO son de modelo: son (1) experimentación diseñada, (2) alcance de
datos, (3) proceso/gobernanza, (4) afinación de cola larga.

## Advertencias críticas (pueden estar mordiendo HOY)

1. **|ε| observacional = cota superior.** Bray/Sanders/Stamatopoulos 2024
   (389,890 precios aleatorizados): ε experimental −0.34 vs observacional
   −1.97; ni IV con costo reconcilió. Matiz a favor: nuestra palanca es lista
   administrada B2B (menos confusión promocional que scanner data). Defensa:
   aleatorizar los pasos ±4% (ver Tier 1).
2. **Replay/event-study con TWFE está sesgado** en adopción escalonada
   (Callaway–Sant'Anna es el fix) y los FRENOS se disparan tras anomalías ⇒
   candidato #1 a regresión a la media; evaluar contra control emparejado, no
   contra el nivel previo del propio SKU. Jamás matching por nivel pre-cambio
   (Daw & Hatfield).
3. **WAPE en cola larga premia pronosticar CERO** (óptimo del error absoluto =
   mediana; con >50% semanas en cero, la mediana es 0). Evaluar intermitentes
   con RMSSE / skill-vs-naive, segmentado por ADI/CV².
4. **SE de FE-PPML sesgados a la baja** (Weidner & Zylkin) ⇒ el blend EB
   encoge menos de lo debido; corrección de sesgo o bootstrap.

## Plan priorizado

### Tier 1 — antes del primer cambio real (barato, crítico)
> Estado (2026-07-27): T1.2 y T1.5 APROBADOS e implementados (ciclo.py,
> clasificar_series + tope lumpy). T1.1 APROBADO (15%, frenos/reversiones exentos, KVIs participan) e implementado.
> T1.3 y T1.4 DESCARTADOS por el negocio ("no considerar").
- **Holdout aleatorio 10–20% por ciclo** dentro de los SKUs elegibles
  (estratificado por familia/volumen; nunca control de la misma familia del
  tratado). Convierte cada ciclo en experimento (patrón Amazon) y es la única
  defensa real contra la advertencia #1.
- **Cerrar loop proyección→realidad por ciclo**: snapshot inmutable de la
  proyección al decidir + variance al cierre (extensión valida_decisiones.py).
- **Tabla MAP por proveedor** como guardrail duro (Hikvision tiene MAP formal;
  violar = riesgo de la distribución). Cruce con "sobrestock manda": si MAP
  impide bajar, la salida es otra palanca, no violar MAP.
- **Matriz de aprobación 3 niveles** (dentro de guardrails+alta ⇒ PM en lote;
  KVI o >2pts ⇒ gerente; cruce de regla o >$X ⇒ comité mensual) + **métricas
  de adopción**: acceptance/override rate con reason codes (referencia: 33%→17%
  overrides; industria implementa solo 32% de aumentos planeados).
- **Clasificación ADI/CV²** (smooth/erratic/intermittent/lumpy) + evaluación
  RMSSE/skill segmentada ⇒ insumo de la escalera de confianza.

### Tier 2 — modelo (semanas)
> Estado (2026-07-27): T2.1 IMPLEMENTADO (duelo por clase en forecast.py: SBA
> gana en errática 4/4 y lumpy 3/4 → sustituye en 8,525 SKUs; intermitente 2/4
> y suave 1/4 → GBM se queda; Δ proyectado bajó a +$121.6K = honestidad).
> Pass-through por EVENTO APROBADO e implementado (revisar_costos en el cron).
> T2.2 HECHO: bandas cuantílicas por SKU en suave+lumpy (7,403 SKUs; en suave
> misma cobertura con banda 44% más angosta). T2.3 HECHO: replay blindado
> (controles limpios + filtro RTM: 2/3 de eventos excluidos; subidas 2-3%
> ganan 59%/+11.9%). T2.5 HECHO: Chronos-Bolt NO entra (vigente gana 3 clases,
> empate en suave) — queda como vara periódica. T2.7 HECHO: DML θ global −0.57
> vs PPML −1.05 (menos elástico, mismo signo; alto triangula, bajo/medio
> divergen) ⇒ tercera línea de evidencia de que |ε| es cota superior; el
> holdout experimental dará el veredicto final. TIER 2 COMPLETO.
> T2.4 chequeo de canibalización EJECUTADO — hallazgo con datos propios:
> ε agregada por proveedor << ε SKU en 4 de 8 grandes (Hikvision −0.41 vs
> −0.97; Panduit −0.19 vs −0.91; Ugreen −0.15 vs −1.08; United Radio −0.26 vs
> −0.98) ⇒ sustitución interna fuerte: subir un SKU pierde menos a nivel
> proveedor de lo que el modelo por SKU sugiere. Uso en reglas: PENDIENTE de
> aprobación. Resto del tier: pendiente.
- Croston/SBA/TSB (statsforecast) como u0/benchmark de cola larga.
- Cuantiles condicionales (GBM loss="quantile") en vez de bandas por tercil.
- Callaway–Sant'Anna + chequeo de pre-trends en el replay de 24k cambios.
- Chequeo de canibalización: ε agregado por familia/proveedor vs ε SKU (si
  |ε familia| << |ε SKU|, mucho del efecto es sustitución interna).
- Chronos-Bolt zero-shot como tercer voto de u0 y vara FVA (candidato a
  reemplazar AWS Forecast: misma casa, generación siguiente, corre local).
- Pass-through de costo por EVENTO (defensa de margen diaria vía
  seguimiento_frenos; no viola cadencia si solo defiende piso).
- DML (dml.py, ya especificado) + corrección de SE del PPML.
- Event-study con ventana de lavado por stockpiling/forward-buying B2B
  (excluir 2–4 semanas alrededor del cambio).

### Tier 3 — alcance de datos y proceso (meses)
- **Waterfall de precio de bolsillo** (lista→factura→bolsillo por
  cliente/vendedor; fuga típica 20–30%) — prerequisito del win-rate etapa 2.
- Inteligencia competitiva: 20–50 KVIs a mano en 3–5 competidores (input, no
  matching automático); Octopart para componentes.
- Ventana 2018+ (estacionalidad real) + ε por temporada.
- Jerarquía temporal semanal↔mensual (reconcilia u0 con AWS coherentemente).
- Jerarquía bayesiana completa por SKU (numpyro; posterior ⇒ abstención).
- Cold start por atributos — re-discutir regla "sin marca/línea" SOLO para
  forecast (predictivo); para elasticidad (causal) sigue vetada.
- Etapa 2 win-rate/deal guidance; incentivos de ventas ligados a realización.

## Antipatrones a vigilar
Spreadsheet elasticity (validaciones de 2 meses en Excel); aumentos parejos de
catálogo; matching automático de competidor; set-and-forget; caja negra (cada
recomendación con reason code — ya cumplimos, preservar); ML prediciendo ε
directo; muerte por piloto (criterios go/no-go ANTES de arrancar); exceso de
capas de aprobación (cada capa ≈ −12% de cierre); sobre-leer resultados de un
SKU individual (decidir sobre agregados por celda).

## Metas de referencia
Implementación de cambios planeados >70% (industria: 32%); overrides a la baja
con 100% reason code; +2–4 pts de margen en ingresos afectados (SparxIQ) /
+4–8% margen en pilotos (McKinsey).
