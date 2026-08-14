# Guía de Colaboración — Motor de Precio Óptimo v3

## 🚀 Inicio rápido para colaboradores

### 1. Clonar el repositorio
```bash
git clone https://github.com/AldoGarfias/Analisis-de-precios.git
cd Analisis-de-precios
```

### 2. Configurar el entorno Python
```bash
python3 -m venv .venv
source .venv/bin/activate          # en macOS/Linux
# o: .\.venv\Scripts\activate      # en Windows

pip install -r requirements.txt
```

### 3. Configurar variables de entorno
```bash
cp .env.example .env.local
# Edita .env.local y llena los valores (credentials, endpoints)
# IMPORTANTE: .env.local está en .gitignore — NUNCA la subas
```

### 4. Ejecutar el pipeline
```bash
python run.py                       # pipeline completo (260s)
python reporte_top.py              # genera HTML del reporte
```

---

## 📊 Estructura del proyecto

```
├── run.py                    # Punto de entrada: orquesta el pipeline
├── panel.py                  # Extracción y limpieza de datos
├── modelo.py                 # Elasticidad, análisis económetrico (PPML)
├── analisis_eps_sku.py       # Elasticidad por SKU
├── validar.py                # Backtest de forecast
├── forecast.py               # Predicción de demanda (GBM + SBA)
├── analisis_canasta.py       # Anclas de canasta (efecto cruzado)
├── analisis_canal.py         # Mix online/vendedor
├── escenarios.py             # Motor principal: genera recomendaciones
├── auditor.py                # Auditoría de costos (política de factor)
├── reporte_top.py            # Genera HTML interactivo
├── competencia.py            # Análisis de precios competencia (matching)
├── equivalentes.py           # Similares entre marcas rivales
│
├── data/                     # Datos (NO subir a Git — en .gitignore)
│   ├── panel.parquet
│   ├── reporte61/            # Catálogo, descripciones, códigos activos
│   └── competencia/          # Matching, análisis de competencia
│
├── out/                      # Salidas (NO subir a Git)
│   ├── recomendaciones.csv   # Output principal: recomendaciones por SKU
│   ├── escenarios.csv        # Detalle de decisiones
│   ├── reporte_precios.html  # Reporte interactivo
│   └── ...
│
├── docs/                     # Documentación
│   ├── ARQUITECTURA_V3.md    # Diseño técnico
│   ├── CAMBIOS_PARA_TI.md    # Historial de cambios
│   └── ...
│
└── requirements.txt          # Dependencias Python
```

---

## 🔄 Flujo de trabajo colaborativo

### Crear una rama para tu tarea
```bash
git checkout -b feature/tu-tarea
# Ejemplo: feature/mejorar-elasticidad, fix/bug-forecast
```

### Hacer cambios y commitear
```bash
git add <archivos>
git commit -m "Descripción clara del cambio (imperativo, presente)"
# Ejemplo: "Agregar reranker Qwen3 al matching de competencia"
```

### Push y Pull Request
```bash
git push origin feature/tu-tarea
# Luego abre PR en GitHub para revisión
```

### Reglas de commit
- **Mensajes claros en español**: `git commit -m "Tema: descripción del cambio"`
- **Una responsabilidad por commit**: no mezcles refactoring con features nuevas
- **Tested**: corre `python run.py` antes de hacer push (verifica que no quebres el pipeline)

---

## ⚠️ Secretos y configuración local

### NUNCA subas:
- `.env.local` — credenciales, tokens, keys
- `data/` — datos privados de clientes
- `out/` — outputs con información sensible
- `.venv/` — dependencias locales

### Está en .gitignore:
```
.env.local
.env
data/
out/
*.parquet
*.joblib
*.csv (excepto los que ya están en Git)
.venv/
__pycache__/
```

Si accidentalmente subes algo sensible, avisar inmediatamente.

---

## 📋 Tareas colaborativas actuales

### 1. Integración de competencia (Sección 9 pendiente)
- Columnas `compet_*` en `out/recomendaciones.csv`
- Chips visuales (⚔/🕊) en el reporte
- Nueva pestaña "Análisis competencia"
- **Responsable**: —  
- **Status**: Diseño listo, sin implementar

### 2. Juez LLM para casos dudosos
- Mejorar decisiones en casos donde el precio no decide (gap >60%)
- Integrar Cohere API o LLM local (Qwen3)
- **Responsable**: —  
- **Status**: Planeado

### 3. Validación de elasticidad por segmento
- Cross-check de ε con análisis de competencia
- **Responsable**: —  
- **Status**: Abierto

### 4. Documentación de matching (similares/equivalentes)
- Especificación técnica del pipeline de competencia (11 secciones)
- Guardar en `docs/PROPUESTA_COMPETENCIA.md`
- **Responsable**: —  
- **Status**: Especificación completa, no documentada aún

---

## 🧪 Testing y validación

### Antes de hacer PR:
1. **Corre el pipeline**:
   ```bash
   python run.py
   ```
2. **Verifica salidas**:
   ```bash
   ls out/
   # Debe haber: recomendaciones.csv, escenarios.csv, reporte_precios.html
   ```
3. **Comprueba que no quebraste cambios previos**:
   ```bash
   git diff main | less  # revisa tu diff
   ```

### Tests unitarios (si aplica):
- Tests para funciones de elasticidad (`modelo.py`)
- Tests para lógica de recomendaciones (`escenarios.py`)
- Tests para matching (`competencia.py`, `equivalentes.py`)

---

## 📚 Recursos clave

| Documento | Propósito |
|-----------|----------|
| `ARQUITECTURA_V3.md` | Diseño técnico del motor |
| `CAMBIOS_PARA_TI.md` | Historial de cambios por fecha |
| `ONBOARDING.md` | Onboarding técnico (BD, APIs, etc.) |
| `COMO_FUNCIONA_EL_MOTOR.md` | Explicación de flujo |
| `docs/PROPUESTA_COMPETENCIA.md` | (Pendiente) Especificación de competencia |

---

## 🤝 Contacto y preguntas

- **Issues**: Usa GitHub Issues para reportar bugs o sugerir features
- **Discussions**: Para preguntas generales de arquitectura
- **Pull Requests**: Con descripción clara de qué cambias y por qué

---

## 📈 Métrica de éxito

Cada cambio debe:
1. ✅ No quebrar el pipeline (`python run.py` sin errores)
2. ✅ Mantener o mejorar la calidad de recomendaciones
3. ✅ Estar documentado en `CAMBIOS_PARA_TI.md`
4. ✅ Tener commit message claro

---

**Última actualización**: 2026-08-14  
**Rama actual**: main  
**URL**: https://github.com/AldoGarfias/Analisis-de-precios
