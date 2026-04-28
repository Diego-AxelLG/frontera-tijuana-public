# Schema dimensional — frontera-tijuana

Modelo Kimball: 5 dimensiones limpias + 3 facts. Schema dedicado `frontera` (no `public`) para aislar del resto de la DB.

DDLs versionados en [`../sql/schema/dimensions.sql`](../sql/schema/dimensions.sql) y [`../sql/schema/facts.sql`](../sql/schema/facts.sql).

---

## ERD

```mermaid
erDiagram
    dim_port ||--o{ fact_border_crossing : "FK port_id"
    dim_port ||--o{ fact_transborder : "FK port_id"
    dim_port ||--o{ fact_wait_time : "FK port_id"

    dim_mode ||--o{ fact_border_crossing : "FK mode_id"
    dim_mode ||--o{ fact_transborder : "FK mode_id"

    dim_commodity_hs2 ||--o{ fact_transborder : "FK commodity_id"

    dim_date ||--o{ fact_border_crossing : "FK date_id"
    dim_date ||--o{ fact_transborder : "FK date_id"
    dim_date ||--o{ fact_wait_time : "FK date_id"

    dim_lane_type ||--o{ fact_wait_time : "FK lane_type_id"

    dim_port {
        smallint port_id PK
        varchar port_canonical_name UK
        integer port_code UK "ej. 2502 = Otay Mesa"
        varchar country "Mexico|Canada"
        varchar border "US-Mexico|US-Canada"
        varchar state
        numeric latitude
        numeric longitude
        boolean is_in_corridor "TRUE para SY, Otay, Tecate"
    }

    dim_mode {
        smallint mode_id PK
        varchar mode_canonical_name UK
        varchar source_dataset "border_crossing|transborder|both"
    }

    dim_commodity_hs2 {
        smallint commodity_id PK
        smallint hs2_code UK "1-99 (HS-2)"
        varchar hs2_description_short
        text hs2_description_full
        varchar sector_grouping "Automotriz, Electrónica, Médico, ..."
    }

    dim_date {
        integer date_id PK "yyyymmdd"
        date full_date UK
        smallint year
        smallint month
        varchar month_name
        smallint day
        smallint day_of_week "1=lunes ISO, 7=domingo"
        varchar day_of_week_name
        smallint week_of_year
        char year_month "YYYY-MM"
    }

    dim_lane_type {
        smallint lane_type_id PK
        varchar lane_canonical_name UK
        varchar vehicle_category "Commercial|POV|Pedestrian"
        boolean is_trusted_traveler "FAST/NEXUS/SENTRI/Ready"
    }

    fact_border_crossing {
        smallint port_id PK_FK
        smallint mode_id PK_FK
        integer date_id PK_FK
        bigint crossing_count
        timestamptz loaded_at
    }

    fact_transborder {
        smallint port_id PK_FK
        smallint mode_id PK_FK
        smallint commodity_id PK_FK
        integer date_id PK_FK
        varchar direction PK "Import|Export"
        varchar container PK "Containerized|Not Containerized|Unknown"
        varchar production_location PK "Domestic|Foreign|N/A"
        bigint value_usd
        bigint weight_kg "nullable"
        bigint freight_charge_usd "nullable"
        timestamptz loaded_at
    }

    fact_wait_time {
        smallint port_id PK_FK
        smallint lane_type_id PK_FK
        integer date_id PK_FK
        numeric waiting_avg_minutes
        timestamptz loaded_at
    }
```

> **Nota sobre `data_source`**: esta columna distingue las dos fuentes que coexisten en `fact_transborder` (ver ADR-002). Las queries del prebake la usan como filtro principal:
> - Análisis nacional / denominadores: `WHERE data_source = 'bts_national'`
> - Análisis del corredor (más reciente): incluye también `'sandag_mirror'`

---

## Dimensiones

### `dim_port` (94 filas)
Catálogo de puertos terrestres de las fronteras US-Mexico y US-Canada. Incluye:
- **27 puertos físicos** con coordenadas (San Ysidro, Otay Mesa, Tecate, Calexico East, Laredo, etc.)
- **District codes y XX residuals**: aggregates oficiales de BTS que TransBorder usa para datos sin port_code identificable. Recuperar este ~13% del valor era crítico para reportes confiables (ver [ADR-003](./decisions/003-port-codes-sinteticos-xx.md)).

Campos clave:
- `port_code`: código BTS canónico (ej. `2502` Otay Mesa, `2503` Tecate, `2504` San Ysidro). Único; sirve de pivote para imputar nombres NULL.
- `is_in_corridor`: TRUE solo para San Ysidro, Otay Mesa y Tecate. Filtro principal del dashboard.

### `dim_mode` (15 filas)
Catálogo de modos de transporte canonizados. Resuelve el solapamiento entre nombres de Border Crossing ("Trucks") y TransBorder ("Truck") — convergen a un único `Truck` con `source_dataset = 'both'`. Lookup oficial DISAGMOT en [`scripts/lookups/disagmot_lookup.json`](../scripts/lookups/disagmot_lookup.json) (ver [ADR-004](./decisions/004-disagmot-6-rail.md)).

### `dim_commodity_hs2` (97 filas)
Capítulos del Harmonized System a 2 dígitos (1–99, sin HS-77 reservado). NO HS-6: la cardinalidad explota a 5K+ valores y el SANDAG mirror solo publica HS-2 agregado. HS-2 es el mínimo común honesto entre fuentes (ver [ADR-005](./decisions/005-commodity-hs2-no-hs6.md)). Cada fila lleva un `sector_grouping` de alto nivel para visualización (Automotriz, Electrónica, Médico, Agro, Energía, Textil, Otros).

### `dim_date` (11,073 filas)
Calendario denso 1996-01-01 → fecha actual. PK como entero `yyyymmdd` (ej. `20260423`) para queries auto-documentadas (`WHERE date_id = 20260423`). Pre-computa year, month, week_of_year, day_of_week, is_weekend para evitar funciones temporales en queries del prebake. Para tablas mensuales (transborder, border_crossing), la FK apunta al día 1 del mes.

### `dim_lane_type` (6 filas)
Tipos de carril CBP normalizados desde el feed SANDAG. Cruce de 3 `vehicle_category` (Commercial, POV, Pedestrian) × programas (FAST, NEXUS/SENTRI, Standard, Ready). El flag `is_trusted_traveler` agrupa SENTRI/NEXUS/Ready/FAST para análisis de programas vs estándar.

---

## Facts

### `fact_transborder` (1,196,239 filas)
Valor en USD del comercio terrestre por puerto × modo × HS-2 × dirección × tipo de container × origen de producción × mes. Granularidad mensual (date_id = día 1 del mes).

**Granularidad y PK extendido**: la PK incluye `direction`, `container` y `production_location` porque el dataset BTS desdobla filas por estos atributos. Ejemplo observado: misma combinación puerto/modo/commodity/mes con valores distintos para Containerized vs Not Containerized.

**Columna `data_source` (ver [ADR-002](./decisions/002-data-source-flag-fact-transborder.md))**:
- `bts_national`: dato oficial BTS desde Wayback (cubre nacional MX→US, gaps en 2008/2010/2011)
- `sandag_mirror`: dato del mirror SANDAG (cubre solo el corredor SD/Imperial pero llega más reciente)

Ambas fuentes coexisten en la misma tabla con la columna como flag. Las queries del prebake filtran según el análisis:
- Análisis nacional / denominadores: `WHERE data_source = 'bts_national'`
- Análisis del corredor: `WHERE port_id IN (...)` (sin filtro `data_source`, usa lo más reciente)

**Sentinelas**: `production_location = 'N/A'` para Imports (la PK no admite NULL); `container = 'Unknown'` cuando el dataset no clasifica. `weight_kg` y `freight_charge_usd` son nullables (BTS no siempre los reporta para Exports).

### `fact_border_crossing` (65,550 filas)
Conteos mensuales de cruces por puerto × modo. Cobertura más larga (1996–presente) porque la API BTS Border Crossing nunca se rompió — los datos llegan vía `data.transportation.gov` directo.

**Caveat documentado**: San Ysidro deja de reportar Trucks/Trains en 2016-07; la carga migró operativamente a Otay Mesa. No es bug del ETL.

`crossing_count` es BIGINT porque hay puertos-mes con >10M peatones (San Ysidro histórico).

### `fact_wait_time` (25,894 filas)
Promedio mensual de minutos de espera por puerto × lane × día. Granularidad **diaria**, no mensual. Fuente: SANDAG SODA (`5tga-nezt`). Cobertura desde 2023-01-29 (cuando SANDAG empezó a publicar el feed).

`waiting_avg_minutes` es `NUMERIC(6,2)`, admite hasta 9999.99 min — cómodo vs el max observado (190 min). El dataset solo cubre dirección sur→norte (espera para entrar a EE.UU.).

---

## Índices

7 índices físicos + 1 índice parcial. DDL completo en [`facts.sql`](../sql/schema/facts.sql). Patrón general:

| Tabla | Índice | Justificación |
|---|---|---|
| `fact_border_crossing` | `(port_id, date_id)` + `(date_id)` | Filtros típicos: puerto + ventana temporal, o serie agregada |
| `fact_transborder` | `(port_id, date_id)` + `(date_id)` + `(commodity_id, direction)` | Análisis por puerto, agregaciones nacionales, mix de mercancía |
| `fact_wait_time` | `(port_id, date_id)` + `(date_id)` | Comparaciones de espera por puerto y serie temporal |
| `dim_port` | `(port_id) WHERE is_in_corridor = TRUE` | Índice parcial sobre el corridor (3 filas). Acelera EXISTS/IN sin overhead de índice completo sobre boolean baja-cardinalidad |
