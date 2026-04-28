# ADR 002 — Columna `data_source` en `fact_transborder` (vs tabla separada)

**Fecha:** 2026-04-26 · **Estado:** decidido

## Contexto

`fact_transborder` se carga desde dos orígenes (ver ADR 001):
- BTS nacional (mayoría: 1,192,871 filas)
- SANDAG mirror (minoría: 3,368 filas, solo 4 meses)

Necesitamos distinguirlos en queries (denominador nacional vs corridor) y trazabilidad operativa. Tres patrones posibles.

## Decisión

Agregar columna `data_source VARCHAR(15) NOT NULL DEFAULT 'bts_national' CHECK (data_source IN ('bts_national', 'sandag_mirror'))` a `fact_transborder`.

```sql
ALTER TABLE frontera.fact_transborder
    ADD COLUMN data_source VARCHAR(15) NOT NULL DEFAULT 'bts_national'
        CHECK (data_source IN ('bts_national', 'sandag_mirror'));
```

## Alternativas consideradas

### Alt A — Tabla separada `fact_transborder_sandag`

- ✓ Aislamiento físico, cada fuente con su propio schema si necesitase divergir.
- ✗ Cualquier query corridor o "rolling 12 meses" requiere `UNION ALL` de dos tablas — código dashboard más complejo y propenso a errores (olvidar la unión = números mal).
- ✗ Si en el futuro agregamos una tercera fuente (e.g., MEXSTATE detail), explosión de tablas.

### Alt B — Vista materializada que une ambas

- Mismo problema de Alt A (dos tablas físicas) + la complicación adicional de mantener la vista actualizada.

### Alt C — Solo `loaded_at` (TIMESTAMPTZ) y derivar la fuente

- ✗ Requiere convención implícita (ej. "todo lo cargado entre fechas X-Y vino de SANDAG"). Frágil — un re-load rompe la convención.
- ✗ Cualquier query necesita conocer las fechas de carga, antipatrón.

## Consecuencias

### Positivas

- Queries triviales: `WHERE data_source='bts_national'` para cosas nacionales, sin filtro para corridor.
- Una sola tabla = índices únicos, PK consistente, COPY simple.
- `CHECK` constraint hace imposible inyectar valores fuera del dominio.
- Default `'bts_national'` significa que el `INSERT` nacional puede omitir la columna (compat con código existente).

### Negativas

- La PK de `fact_transborder` no incluye `data_source`. Esto significa que **no se puede** tener la misma `(port, mode, commodity, date, direction, container, prod_loc)` desde ambas fuentes — violaría PK. Mitigación: el ETL hace pre-check `WHERE date_id BETWEEN ... AND data_source='bts_national'` y aborta si hay overlap antes de cargar SANDAG.
- Si en el futuro queremos cargar la misma fila desde múltiples fuentes (para reconciliación), habría que extender la PK con `data_source` o resolver la duplicidad antes del load.

### Convenciones de query

- **Análisis nacional / denominadores**: `WHERE data_source = 'bts_national'`.
- **Análisis del corredor SD/Tijuana**: sin filtro adicional (incluye SANDAG mirror para Dec 2024+).
- **Reconciliación entre fuentes**: queries que comparen `value_usd` entre los dos `data_source` para los mismos puertos en el mismo período (cuando aplique).
