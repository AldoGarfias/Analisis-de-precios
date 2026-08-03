# ¿El canal EN LÍNEA acepta mejor los aumentos? (2026-08-01)

Hipótesis del usuario: las ventas en línea (cliente solo, sin ejecutivo
sensibilizado) aceptan más los aumentos que las ventas por vendedor; y el
vendedor amortigua los aumentos con descuento adicional (d1 > 20 sin proyecto).

Script persistente: `analisis_canal.py` → `data/analisis_canal.parquet`.
Diseño fijado ex-ante (juez: mediana pareada + test de signos; ajuste por
mercado DEL PROPIO canal; solo eventos con venta previa en ambos canales).
Canal por `concepto` ("línea" = ventas en línea + V. Linea Sinosure, sin
proyecto; "vendedor" = resto sin garantías/proyectos). El folio de pedido 576
NO viaja en reporte_61 (solo folios de factura FA).

## Resultados (9,805 eventos pareados, subidas de lista ≥2% aisladas, 2024→hoy)

**H1 — aceptación: CONFIRMADA direccionalmente, tamaño modesto, gradiente claro.**

| Magnitud del alza | n | Retención EN LÍNEA | Retención VENDEDOR | Dif |
|---|---|---|---|---|
| 2-5% | 5,056 | 0.936 | 0.868 | **+0.046** |
| 5-10% | 1,739 | 0.862 | 0.816 | +0.021 |
| >10% | 2,037 | 0.689 | 0.601 | +0.008 |
| Global | 9,805 | 0.879 | 0.825 | +0.014 (p-signos 0.0017) |

- ε implícita al aumento: línea **−2.07** vs vendedor **−3.04** (nivel absoluto
  no comparable con la ε del panel — ventana corta sin FE; lo que informa es la
  BRECHA entre canales).
- Estable en el tiempo (1ª mitad +0.012, 2ª +0.015).
- CAVEAT honesto: la media recortada da −0.009 (el canal vendedor tiene colas
  pesadas de pedidos grandes que inflan ratios post/pre) — por eso el juez
  ex-ante era mediana + signos; la conclusión depende de esa elección, que se
  fijó ANTES de ver resultados.
- Lectura: en subidas CHICAS (2-5%) el cliente en línea casi no lo nota
  (retiene 94%); el canal con vendedor reacciona el doble. En subidas >10%
  ambos canales huyen y la ventaja se comprime.

**H2 — amortiguación del vendedor: CONFIRMADA en la masa, no en el evento típico.**

- Share de renglones con d1>20 (sin proyecto) tras el aumento, media pooled:
  vendedor **2.3% → 3.5% (+1.15 pts, +49% relativo)** vs línea 3.8% → 4.0%
  (+0.16 pts). El evento MEDIANO no cambia (los descuentos extra son raros);
  el efecto vive en una cola: en ~12% de los eventos el share del canal
  vendedor salta >+5 pts.
- En esa cola, la retención del canal vendedor es **0.975 vs 0.846** en el
  resto: cuando el vendedor amortigua con descuento, SALVA el volumen — a
  costa del margen del aumento.
- Pass-through al neto unitario: vendedor **0.96** vs línea **1.03** — el
  vendedor regala ~4 pts de cada alza; en línea el aumento llega completo
  (y algo más: mezcla).

## IMPLEMENTADO (usuario 2026-08-01: "Aplica las 4")

1. **Mezcla de canal en el motor** (`analisis_canal.py mezcla` →
   `data/mezcla_canal.parquet`, paso de run.py antes de escenarios):
   ajuste SOLO de confianza en SUBIR ≤4pts — pct_linea ≥70% y ≥10u/26sem:
   media→alta; ≤30%: alta→media. Jamás toca dirección ni precio. SIN chip en el
   resumen (usuario 2026-08-01: demasiadas etiquetas — la regla trabaja en
   el fondo); cuando fue DETERMINANTE, columna `canal_ajuste` en recos y
   explicación completa en el panel detallado. Primera corrida: 1,548 ↑ / 91 ↓.
2. **Bandera 🚩 descuento amortiguador** (`monitoreo.py`): en el veredicto de
   4 semanas de un SUBIR, si el share d1>20 del canal vendedor saltó >5pts
   (≥10 renglones por lado), columna `desc_amortiguador=True` — se MARCA (la
   utilidad realizada es real) pero la ε aprendida de ahí está contaminada.
   Insumo: `data/canal_semanal.parquet` (refrescado con la mezcla).
3. **Reporte de gobernanza** (`analisis_canal.py fuga`, semanal en el cron de
   lunes): alzas de 12 semanas donde el vendedor amortiguó → 
   `out/fuga_descuentos.csv/.html` para dirección comercial. Primera corrida:
   23 eventos, $11,584 regalados.
4. **Etapa 2 win-rate en marcha**: `extract_cotizaciones.py` extrae
   estatus='Cotizacion' 2024→hoy (reciente→atrás, API titular/Aurora suplente,
   ~16.5K renglones/día). El modelo bid-response (lecciones v1: pesos, dedup
   cliente-SKU-semana, rel_precio vs lista aplicable) se construye al terminar.

## Pendiente (no implementado)

- Retador de ε por mezcla de canal (ε menos elástica para SKUs
  mayoritariamente en línea) — requiere juez OOT fijado ANTES de correr,
  como la capa SKU. Se evaluará cuando haya ciclos aplicados con la bandera
  de amortiguador limpia.
- "Normalizar" las alzas anticipando el descuento del vendedor: DESCARTADO
  (espiral de descuento / erosión de la integridad de lista — teoría y
  decisión del 2026-08-01).
