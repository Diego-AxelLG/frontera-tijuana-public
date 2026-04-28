# Architecture — Frontera Tijuana–San Diego

## Stack

| Capa | Componente |
|---|---|
| Storage | Postgres 17 local (host accesible vía Tailscale `100.123.166.123:5432`) |
| Schema | Schema dedicado `frontera` en DB `frontera_tijuana` |
| ETL | Python 3.14 con `pandas`, `psycopg2-binary`, `requests`, `beautifulsoup4`, `tqdm` |
| Sources | Archivos planos BTS (vía Wayback por geo-block); SANDAG SODA API; data.transportation.gov |

## Modelo dimensional

5 dimensiones + 3 facts en estrella. Tablas singulares (Kimball convention).

```
                         dim_date (date_id INT yyyymmdd)
                              ↓
        ┌─────────────────────┼─────────────────────────┐
        ↓                     ↓                         ↓
  fact_border_         fact_transborder           fact_wait_time
   crossing            (1.20M filas)              (25.9k filas)
  (65.5k filas)              ↓                         ↓
        ↓                    ↓                         ↓
   ┌────┴────┐          ┌────┼────┐               ┌────┴────┐
   ↓         ↓          ↓    ↓    ↓               ↓         ↓
dim_port  dim_mode   dim_   dim_  dim_         dim_port  dim_lane_
                    port  mode commodity                  type
                                hs2
```

### Tablas

| Tabla | Filas | Tamaño | Granularidad |
|---|---:|---:|---|
| `dim_date` | 11,073 | 600 KB | día (1996-01-01 → CURRENT) |
| `dim_port` | 94 | 24 KB | 27 físicos + 67 aggregates (district codes + XX residuals) |
| `dim_mode` | 15 | 6 KB | modos canonicalizados con `source_dataset` flag |
| `dim_commodity_hs2` | 97 | 24 KB | capítulos HS-2 (1-76, 78-98, sin HS-77 reservado) |
| `dim_lane_type` | 6 | 5 KB | carriles CBP (Commercial/POV/Pedestrian × Fast/Standard) |
| `fact_border_crossing` | 65,550 | 6.4 MB | mensual · puerto · modo |
| `fact_transborder` | 1,196,239 | 333 MB | mensual · puerto · modo · commodity HS-2 · dirección · container · production |
| `fact_wait_time` | 25,894 | 2.9 MB | diario · puerto · carril |

## Flujo del pipeline

```
1. Carga inicial de dimensiones      → etl/01_load_dimensions.py
2. Bajar 108 ZIPs vía Wayback        → etl/02_download_national.py (+ 03_retry)
3. Extraer dot3 CSVs por mes         → etl/04_extract_national_zips.py
4. ETL fact_transborder              → etl/05_load_fact_transborder.py
   - Parte 1: 88 ZIPs nacionales
   - Parte 2: SANDAG adapter (Dec 2024 + Q1 2025)
5. ETL fact_border_crossing          → etl/06_load_fact_border_crossing.py
6. ETL fact_wait_time                → etl/07_load_fact_wait_time.py
```

Cada script es **idempotente** — re-ejecutar con `python etl/<script>.py` produce el mismo estado. Las facts hacen `TRUNCATE` antes del `COPY`. Las dimensiones usan `ON CONFLICT DO NOTHING` o `TRUNCATE ... RESTART IDENTITY CASCADE`.

## Convenciones

- **snake_case** en todo (tablas, columnas, archivos Python).
- **Singular** en nombres de tabla (`dim_port`, no `dim_ports`) — convención Kimball.
- **Schema dedicado `frontera`** (no `public`) — aislamiento, permisos, evita colisión con extensiones.
- **`date_id` como `INTEGER yyyymmdd`** (ej. `20240301` para March 1, 2024) — patrón Kimball, queries auto-documentadas.
- **PK compuesta** en facts incluye todas las dimensiones que pueden splitear filas (ej. `fact_transborder` tiene `container` y `production_location` en PK porque el source las desdobla).
- **Sentinela `'N/A'`** para `production_location` cuando es NULL (el PK no admite NULL).
- **Nullable nominal**: `weight_kg` y `freight_charge_usd` pueden ser NULL (export sin weight reportado).
- **`loaded_at TIMESTAMPTZ DEFAULT now()`** en cada fact para trazabilidad de cargas.
- **`is_aggregate` en `dim_port`**: TRUE para district aggregates / XX residuals; rankings de "top puertos físicos" filtran `WHERE is_aggregate=FALSE`.
- **`data_source` en `fact_transborder`**: `'bts_national'` o `'sandag_mirror'`. Análisis nacional filtra a `bts_national`; análisis del corridor usa todo.

## Cómo correr el pipeline desde cero

### Prerequisites

- Python 3.13+
- Postgres 15+ accesible (la DB `frontera_tijuana` puede no existir aún)
- Conexión a internet (Wayback Machine + SANDAG SODA API)
- Acceso desde IP **NO geo-bloqueada por Akamai en bts.gov** — alternativa: usar Wayback (lo que hace el pipeline)

### Setup

```bash
git clone <repo>
cd frontera-tijuana
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # editar con credenciales reales (PG_HOST, PG_USER, PG_PASSWORD)
```

### Crear DB y schema

```bash
# Crear database
PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -U "$PG_USER" -d postgres \
  -c "CREATE DATABASE frontera_tijuana;"

# Aplicar schema
PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -U "$PG_USER" -d frontera_tijuana \
  -f schema/01_schema.sql
```

### Cargar dimensiones

```bash
python etl/01_load_dimensions.py
```

Esto puebla las 5 dimensiones (~11.3k filas dim_date + 27 ports + 14 modes + 97 commodities + 6 lanes).

**Nota**: necesita `data_samples/border_crossing_full.csv` (ignorado por git, regenerable):

```bash
mkdir -p data_samples
curl -sL -o data_samples/border_crossing_full.csv \
  'https://data.transportation.gov/api/views/keg4-3bc2/rows.csv?accessType=DOWNLOAD'
```

### Bajar TransBorder nacional (~30-40 min)

```bash
python etl/02_download_national.py        # discovery + descarga vía Wayback
python etl/03_retry_downloads.py          # retry agresivo (recupera ~25 falsos negativos)
python etl/04_extract_national_zips.py    # extrae dot3 CSVs por mes
```

Resultado: ~98 ZIPs en `data_raw/transborder_national/` (~3 GB) + 178 dot3 CSVs en `extracted/`.

### Cargar facts

```bash
python etl/05_load_fact_transborder.py    # nacional + SANDAG adapter
python etl/06_load_fact_border_crossing.py
python etl/07_load_fact_wait_time.py
```

### Verificación

```bash
PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -U "$PG_USER" -d frontera_tijuana <<EOF
SELECT 'fact_transborder' tbl, count(*) FROM frontera.fact_transborder
UNION ALL SELECT 'fact_border_crossing', count(*) FROM frontera.fact_border_crossing
UNION ALL SELECT 'fact_wait_time', count(*) FROM frontera.fact_wait_time;
EOF
```

Esperado: ~1.20M / 65.5k / 25.9k filas.

## Decisiones arquitectónicas

Ver `docs/decisions/` para ADRs detallados:

- `001` — Fuente TransBorder: nacional vs SANDAG mirror
- `002` — Flag `data_source` en `fact_transborder` (vs tabla separada)
- `003` — Códigos sintéticos `_99` para distritos XX residuales
- `004` — Corrección DISAGMOT 6 = Rail (con docs BTS oficiales)
- `005` — Granularidad commodity HS-2 (no HS-6)
- `006` — CBX (Cross Border Xpress) fuera de scope
