# Evaluación exhaustiva: ¿qué modelo de forecast mensual conviene? (2026-07-31)

Pedido del usuario: "evaluación/investigación exhaustiva de qué modelo es el más
adecuado para nuestro tipo de datos y negocio, después una tabla comparativa:
lo que hacemos con AWS, la vía propia y el modelo o modelos seleccionados".

Método: (a) dos investigaciones web independientes (estado del arte M4/M5,
demanda intermitente, modelos fundacionales 2024-2026, práctica industrial;
y literatura de combinación/selección + diseño de jueces); (b) duelos
empíricos sobre NUESTROS datos con juez fijado antes de correr (6 meses
out-of-time, WAPE sobre venta total mensual por SKU, ~13,400 SKUs por mes).

## Duelos empíricos (nuestros datos, 6 meses OOT: ene-jun 2026)

| Candidato | WAPE prom | Meses ganados al ingenuo | Veredicto |
|---|---|---|---|
| Ingenuo (mes anterior) — piso | 0.432 | — | baseline permanente |
| Estacional simple (mediana 3m × índice) | ~0.45 | 3/6 | NO entra |
| Ingenuo estacional (re-estacionalizado) | ~0.43 | 2/6 | NO entra (31 meses ⇒ estacionalidad = ruido) |
| GBM residual mensual (lags/nivel/intermitencia/mes/tendencia) | 0.400 | 4/6 | bueno pero frágil en meses anómalos |
| Selección por clase (GBM en suave/errática/interm., ingenuo en lumpy) | 0.410 | 4/6 | NO mejora a grano mensual |
| **ENSAMBLE 50/50 GBM + ingenuo** | **0.394** | **6/6** | **ADOPTADO** |

El ensamble ganó TODOS los meses, incluidos los dos donde el GBM solo perdía
(mayo, y junio = mes anómalo de aniversario): el ingenuo estabiliza al GBM en
meses raros; el GBM aporta tendencia donde el ingenuo se rezaga.

## Lo que dice la evidencia externa (con fuentes en los reportes de investigación)

1. **El margen ganable a grano SKU-mensual es de UN DÍGITO.** M5: el ganador
   mundial mejoró 22% en el agregado pero solo ~3% a nivel producto-tienda;
   solo 48% de 5,507 equipos le ganó al naive. Morlidge (300K+ pronósticos
   reales): 52% de los pronósticos corporativos son PEORES que el naive, y
   ganarle >10-15% es excepcional. **Nuestro −8.8% (RAE 0.91) está en la banda
   alta de lo alcanzable** — no esperar WAPE 0.25 a este grano.
2. **La combinación simple es la práctica con mejor evidencia** (50 años de
   "forecast combination puzzle"; 12 de los 17 mejores de M4 fueron
   combinaciones; media simple ≥ pesos estimados). Ensambles de 2-3 modelos
   capturan casi todo el beneficio.
3. **GBM global es el consenso a nivel SKU** (M5 top-50 = LightGBM global;
   paper 2026 local-vs-global en >40K series intermitentes: los globales con
   GBT baten a la familia Croston). Nuestro GBM residual replica ese diseño.
4. **La vara "ganar 5 de 6 meses" era estadísticamente ciega**: un retador
   genuinamente mejor solo la pasa ~23% de las veces. JUEZ NUEVO (el de las
   competencias M): media GEOMÉTRICA de los ratios WAPE_retador/WAPE_campeón
   (entra si <1 con margen) + significancia por rangos entre los ~13K SKUs
   (ahí vive el poder estadístico, no en 6 meses) + meses anómalos declarados
   ex-ante se winsorizan al segundo peor (no se excluyen en silencio).
5. **En lumpy (43% del catálogo) el punto está casi empatado por diseño**: el
   valor está en los cuantiles (P50/P90) y en la decisión de inventario, no en
   ganarle al naive en WAPE. Perder ahí no es fracaso del modelo, es propiedad
   de la serie.
6. **Fundacionales**: zero-shot NO gana (nuestro examen de Chronos-Bolt y el
   benchmark de Decathlon con 25K productos coinciden); FINE-TUNED sí es
   retador legítimo (Chronos-2 afinado ganó la tabla de Decathlon). Los claims
   de TimeGPT en intermitencia no son reproducibles.
7. **Separar flujo recurrente vs proyecto ANTES de modelar es práctica
   estándar** de la industria (no un truco local) — nuestra regla ya lo hace;
   el siguiente paso industrial es pronosticar el flujo de proyectos aparte
   (pipeline comercial), no ignorarlo.

## TABLA COMPARATIVA FINAL

| | **AWS (del negocio)** | **Vía propia SEMANAL (motor)** | **Vía propia MENSUAL (adoptada hoy)** |
|---|---|---|---|
| Método | ML caja negra (SOW4): outliers→KNN, cuantil P60, cold-start por metadata | GBM residual global + SBA por clase Syntetos-Boylan (duelo FVA ≥3/4) | **Ensamble 50/50: GBM residual mensual + ingenuo** |
| Target | venta total mensual | venta RECURRENTE semanal (sin proyectos) | venta TOTAL mensual (la que rota stock) |
| Horizonte | 5 meses | ciclo (3 semanas) | 5 meses |
| Error medido | WAPE ~36% in-sample (optimista); examen honesto desde ago | WAPE 0.384 OOT semanal (campeón 4/4) | **WAPE 0.394 OOT vs 0.432 del ingenuo (6/6 meses, −8.8%)** |
| Rol en el motor | 2ª opinión informativa + voz del examen (ya NO alimenta meses de stock) | u0 de TODAS las decisiones de precio (bandas por SKU) | 3ª voz del examen CON voto + **vía PRIMARIA de meses de stock v3 (adoptada el mismo día, con filtro de credibilidad)** |
| Fortaleza | ve demanda total, detalle por sucursal | validado por clase, bandas honestas, entrenado en recurrente limpio | robusto a meses anómalos (el ingenuo ancla), transparente, barato |
| Debilidad | P60 sesgado alto, embarra proyectos one-off (caso TXTPH700C), sin examen honesto aún | no pronostica proyectos (por diseño) | margen sobre naive estructuralmente acotado (~9%) |

## Retadores futuros documentados (entran solo por duelo con el juez nuevo)

- **Loss Tweedie/pinball** en el GBM mensual (mejores cuantiles altos en lumpy).
- **Capa MAPA/ADIDA** (combinación de niveles de agregación temporal) SOLO
  para lumpy/intermitente — la única familia con evidencia consistente ahí.
- **Chronos-2 o TabPFN-TS FINE-TUNED** con nuestros 10 años crudos (nunca
  zero-shot); re-afinado semestral. TabPFN-TS si queremos conservar
  covariables (mes, stockout, precio).
- **Estacional propio**: revancha cuando el backfill 2016+ dé 8-10
  repeticiones de cada mes calendario.
- **Cuantiles para lumpy**: P50/P90 como entregable en vez del punto.

## Decisiones tomadas (2026-07-31)

1. ADOPTADO el ensamble 50/50 como "propio" del examen mensual, CON voto.
   El archivo pred_202607 se regeneró con el método adoptado (el estacional
   fallido nunca participó en un examen).
2. JUEZ NUEVO para futuros duelos mensuales: media geométrica de ratios +
   rangos por SKU + winsorización de meses anómalos declarados ex-ante
   (junio 2026 = aniversario queda declarado).
3. Baselines permanentes del examen: ingenuo Y ingenuo estacional.
4. EQUIDAD DE HORIZONTE en el examen: se califica el archivo más reciente
   generado antes de conocer el mes (h≈1 para todos, igual que el ingenuo).
