# Venta cruzada: ¿subir X arrastra a los que se venden con X? (2026-07-31)

Pregunta del usuario: "si subo un precio, ¿cómo afecta a los modelos que se
venden juntos? ¿tiene sentido analizar folios? ¿alguien ya lo hace?"

Método: investigación externa (25+ fuentes: meta-análisis, papers de canasta,
práctica de Revionics/Zilliant/PROS) + estudio empírico propio sobre folios
reales (analisis_canasta.py).

## Respuestas directas

1. **¿Tiene sentido?** Sí — elasticidad CRUZADA de precio; complementos =
   cruzada negativa. La distinción crítica (Manchanda et al. 1999, el paper
   canónico de canasta): **complementariedad ≠ co-incidencia** — dos SKUs
   pueden co-ocurrir sin que el precio de uno mueva al otro (el caso folio de
   proyecto B2B). Nuestro diseño excluye proyectos por eso.
2. **¿Alguien lo hace?** Retail sí (Revionics mide "demand transference";
   el marco KVI/loss-leader vive de esto). **Las suites B2B (Zilliant, PROS,
   Pricefx) NO lo meten al optimizador de precio** — lo tratan como
   cross-sell para vendedores. Medirlo con eventos propios nos pone por
   delante de la práctica B2B comercial.
3. **¿Es de primer o segundo orden?** La evidencia externa: segundo orden EN
   PROMEDIO (cross mediana ±0.10 vs propia −2.6; spin-off ≈9% del efecto
   propio; la mayoría no significativos) **con cola pesada de primer orden**:
   pares ancla-accesorio/cautivos (impresora-cartucho) sostienen modelos de
   negocio enteros (Gabaix-Laibson).

## Nuestro estudio (5,842 pares-evento; 13,445 subidas aisladas; folios
## 2024→hoy sin proyectos/kits; compañeros con lista propia quieta)

| Attach (P(Y|X) en folios) | n | ΔY venta (ajust. mercado) | ε cruzada |
|---|---|---|---|
| 10-20% | 3,537 | −2.6% | −0.81 |
| 20-40% | 1,706 | −2.9% | −0.97 |
| **>40%** | **559** | **−5.2%** | **−2.00** |

Mediana global: ΔX +3.1% → ΔY −2.9% ⇒ ε cruzada ≈ −0.94. **Gradiente
dosis-respuesta limpio** (más attach ⇒ más arrastre): la firma de causalidad.

Lectura honesta de la magnitud: nuestros pares ya son LA COLA (attach ≥10% y
≥30 co-folios) — por eso salen mucho mayores que la mediana de la literatura
(±0.10, que promedia pares débiles). Consistente con la "cola pesada" (H12
del reporte de investigación). Por par individual hay ruido (35% caen fuerte
pero 25% suben fuerte): las medianas por bucket son confiables, el par suelto
no — cualquier regla debe usar los buckets, no pares individuales.

## ROBUSTEZ (objeción del usuario 2026-07-31: "¿no será que el principal
## subió su propio precio y eso tiró ambas ventas?")

Verificado en tres capas: (1) caso TPAR5AC→LBE5ACGEN2 por evento: el ÚNICO
evento donde el radio también subió dio ΔY POSITIVO (+6.2% — la contaminación
diluía, no fabricaba); los dos eventos con lista del radio plana 12 semanas
dieron −18.5% y −23.0%. (2) FILTRO DURO a escala: compañero con lista quieta
t−8..t+4 (ni un movimiento ≥1.5% en 12 semanas) ⇒ 3,654 pares sobreviven y el
efecto persiste: ε global −0.87 (vs −0.94), attach >40%: −2.27 (MÁS fuerte).
(3) El gradiente dosis-respuesta se mantiene. Datos del filtro duro en
data/analisis_canasta_estricto.parquet.

## REGLA APROBADA Y ACTIVA (usuario 2026-07-31: "hagamos el cambio")

**"Margen arrastrado de canasta"** — TAL COMO QUEDÓ IMPLEMENTADA en
`escenarios.py` (ronda 3: esta sección se alinea con el código, que es la
referencia):
1. ANCLAS: mapa vigente de `analisis_canasta.py anclas` (folios de 26 semanas,
   sin proyectos/kits, attach ≥10%, ≥30 co-folios). Margen arrastrado =
   Σ_compañeros (utilidad_semanal_Y × |ε_cruzada del bucket| × Δprecio_X/100).
   El attach NO multiplica: solo elige el bucket de ε (10-20% → 0.80,
   20-40% → 0.78, >40% → 2.27, fronteras right-closed como el pd.cut del
   estudio). La utilidad de compañeros fuera del motor se estima del panel
   (26 semanas), no se cuenta como cero.
2. Umbral definitivo: arrastre ≥ **50%** de la ganancia propia proyectada ⇒
   BLOQUEAR el SUBIR (MANTENER, chip ⚓ ANCLA con el arrastre estimado).
   "Moderar el paso" NO se implementó — solo bloqueo binario. Frenos exentos
   (urgencia de stock > canasta).
3. Para el resto: cross = 0, pipeline intacto (evidencia externa: la mayoría
   de los pares son independientes).
4. Simétrico ("accesorios de canasta": donde subir es MÁS seguro) — idea
   registrada, NO implementada.

Advertencias B2B incorporadas al diseño: proyectos excluidos; el folio B2B es
negociación (el efecto solo se identifica con eventos de LISTA, nunca con
correlación neto-volumen); no doble-contar con la sustitución por proveedor
ya detectada (canibalización); eventos simultáneos del mismo proveedor
filtrados.

Datos: data/analisis_canasta.parquet (par-evento) y
data/analisis_canasta_estricto.parquet (filtro duro — regenerable con
`analisis_canasta.py estricto`, persistido en ronda 3). Refinamientos futuros:
lift dentro de segmento de cliente, asimetría direccional P(Y|X)>>P(X|Y),
PPML con FE para la estimación fina, capa EB por familia de relación.
