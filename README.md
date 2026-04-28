# Frontera Tijuana–San Diego — Pipeline ETL

Pipeline analítico del comercio terrestre entre el corredor San Diego/Imperial (US) y Tijuana/Tecate (MX) basado en datos públicos de BTS Border Crossing, BTS TransBorder y SANDAG. **18 años de cobertura (2007–2026), 1.29M filas, 4 secciones interactivas en el dashboard.**

📊 **Dashboard live**: `<URL del portfolio en producción>/dashboards/frontera`

<p align="center">
  <img src="https://raw.githubusercontent.com/Diego-AxelLG/portfolio-landing/main/public/previews/frontera.svg" alt="El corredor SD/Tijuana — 97.87% del valor pasa por Otay Mesa" width="600"/>
</p>

---

## Las 5 conclusiones del análisis

> Validadas con dato real durante la fase 4 del pipeline. Cada una tiene su métrica explícita y su corte temporal. Números congruentes con los JSONs publicados en el frontend.

1. **El corredor no es 3 puertos: es Otay con 2 satélites.** Otay Mesa concentra **97.87%** del valor económico del corredor SD/Tijuana en 2024. Tecate y San Ysidro suman 2.13%. La asimetría es brutal: Otay mueve **51.7×** más valor que Tecate y **408×** más que San Ysidro.

2. **El nearshoring tiene firma estadística medible.** Comparando 2024 (anualizado) vs 2019 prepandemia: el valor exportado por Otay creció **+32.2%** mientras los camiones solo **+11.7%**. Ratio de densidad económica: **2.75×** — cada camión mueve más mercancía sofisticada.

3. **Mix de mercancía dominado por maquiladora moderna.** Top 3 sectores en Otay 2024 cubren **63.3%** del valor: Electrónica/Maquinaria 34.5%, Automotriz 15.6%, Médico/Precisión 13.2%. Los sectores tradicionales (textil 3.3%, agro 9.2%) pesan menos del 10% individualmente.

4. **FAST no es para todos los días — es para los que importan.** En Otay Mesa Comercial 2024, el lane FAST ahorra **34.0% de tiempo en p95** (peores días) vs el lane Standard. La distribución importa más que el promedio: ahorro p50 = −18.5%, ahorro p95 = −34.0%, ahorro máximo = −31.7%.

5. **El corredor pesa poco a escala nacional, pero crece rápido.** SD/Imperial mueve **7.55%** del valor terrestre Mexico→US (TTM cerrando 2024-11). El otro 92.45% pasa por Texas, Arizona y Calexico Este. Pero el comercio total Mexico→US creció **+36.2%** sobre 2019.

---

## Stack

- **Pipeline**: Python 3.10+, psycopg2, pandas, requests, beautifulsoup4, chardet, tqdm
- **Storage**: PostgreSQL (DWH dimensional, schema `frontera`)
- **Output**: 18 JSONs estáticos pre-bakeados consumidos por el frontend
- **Frontend** (repo separado): Next.js 14 + Recharts ([portfolio-landing](https://github.com/Diego-AxelLG/portfolio-landing))

Ver [`pyproject.toml`](./pyproject.toml) para dependencias exactas.

---

## Arquitectura

```
Fuentes públicas              Pipeline ETL                Storage              Pre-bake          Frontend
─────────────────             ────────────                ────────             ────────          ────────
BTS TransBorder         ─→  scripts/02-04 (descarga)  ─→  fact_transborder  ─┐
SANDAG mirror              scripts/01     (dims)        dim_*               ─┤
BTS Border Crossing     ─→  scripts/06    (cargas)   ─→  fact_border_cross ─┼─→ scripts/08 ─→ 18 JSON ─→ portfolio-landing
SANDAG Wait Times       ─→  scripts/07                ─→  fact_wait_time    ─┘   (Fase 5)         estáticos
```

Detalles: [`docs/pipeline.md`](./docs/pipeline.md) · Schema: [`docs/schema.md`](./docs/schema.md)

---

## Schema dimensional

Modelo Kimball clásico: **5 dimensiones + 3 facts**. Total 1.29M filas.

| Tabla | Tipo | Filas | Cobertura temporal |
|---|---|---:|---|
| `dim_date` | Dimensión | 11,073 | 1996-01 → CURRENT |
| `dim_port` | Dimensión | 94 | 27 puertos físicos + aggregates (district codes + XX residuals) |
| `dim_mode` | Dimensión | 15 | — |
| `dim_commodity_hs2` | Dimensión | 97 | — |
| `dim_lane_type` | Dimensión | 6 | — |
| `fact_transborder` | Hechos | 1,196,239 | 2007–2024-11 (national) / hasta 2025-03 (corridor mixed); gaps en 2008/2010/2011 |
| `fact_border_crossing` | Hechos | 65,550 | 1996-01 → 2026-03 |
| `fact_wait_time` | Hechos | 25,894 | 2023-01-29 → 2026-04 |

ERD completo + descripción de cada tabla en [`docs/schema.md`](./docs/schema.md).
DDLs en [`sql/schema/dimensions.sql`](./sql/schema/dimensions.sql) y [`sql/schema/facts.sql`](./sql/schema/facts.sql).

---

## Decisiones técnicas (ADRs)

Las decisiones no obvias del proyecto están documentadas como Architecture Decision Records:

| # | Decisión | Por qué |
|---|---|---|
| [001](./docs/decisions/001-fuente-transborder-nacional-vs-sandag.md) | Fuente híbrida TransBorder: nacional (bts.gov) + SANDAG mirror | Geo-blocking de bts.gov desde MX; SANDAG mirror cubre el corredor pero no el denominador nacional |
| [002](./docs/decisions/002-data-source-flag-fact-transborder.md) | Columna `data_source` en `fact_transborder` (vs tabla separada) | Las dos fuentes describen el mismo grano. Una columna es 90% más simple y permite filtrar fácilmente |
| [003](./docs/decisions/003-port-codes-sinteticos-xx.md) | Códigos sintéticos `_99` para distritos XX residuales | TransBorder agrupa el ~13% del valor en distritos XX. Recuperarlo era condición para reportes confiables |
| [004](./docs/decisions/004-disagmot-6-rail.md) | DISAGMOT 6 = Rail (corrección con docs BTS oficiales) | Heurística inicial era incorrecta. Lookup oficial > inferencia |
| [005](./docs/decisions/005-commodity-hs2-no-hs6.md) | Granularidad de commodity: HS-2, no HS-6 | HS-6 explota cardinalidad (5K+ valores) y SANDAG mirror solo trae HS-2 agregado. HS-2 es el mínimo común honesto |
| [006](./docs/decisions/006-cbx-fuera-de-scope.md) | Cross Border Xpress (CBX) fuera de scope | Aeropuerto, no comercial terrestre. Distorsiona la narrativa principal |

---

## Limitaciones conocidas

Documentadas en [`docs/KNOWN_GAPS.md`](./docs/KNOWN_GAPS.md). Las críticas:

- **2008, 2010, 2011 sin cobertura TransBorder**: Wayback Machine truncó los ZIP anuales. Los charts del frontend marcan estos años con `ReferenceArea` sombreado en lugar de interpolarlos. 2006 también ausente (formato legacy).
- **Cobertura asimétrica fuente vs período reciente**: BTS TransBorder llega solo hasta 2024-11 (nacional) y 2025-03 (corredor mixto, vía SANDAG mirror). BTS Border Crossing llega a 2026-03. Algunos meses tienen camiones pero no valor económico.
- **San Ysidro Trucks reportados hasta 2016-07**: la carga migró operativamente a Otay; no es bug del ETL.
- **Wait Times solo desde 2023-01-29**: cobertura nativa del feed SANDAG.

---

## Reproducir el pipeline localmente

> Asume Postgres corriendo localmente o accesible vía red, Python 3.10+, y disco para las descargas raw (~3 GB).

```bash
# 1. Clonar y configurar entorno
git clone https://github.com/Diego-AxelLG/frontera-tijuana-public.git
cd frontera-tijuana-public
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configurar variables de entorno
cp .env.example .env
# Editar .env con PG_HOST, PG_USER, PG_PASSWORD, PG_DATABASE, PG_PORT

# 3. Crear schema en Postgres
psql "postgresql://$PG_USER:$PG_PASSWORD@$PG_HOST:$PG_PORT/$PG_DATABASE" \
  -f sql/schema/dimensions.sql \
  -f sql/schema/facts.sql

# 4. Cargar dimensiones (rápido, segundos)
python scripts/01_load_dimensions.py

# 5. Descargar y cargar TransBorder nacional (lento — ~108 ZIPs vía Wayback)
python scripts/02_download_national.py
python scripts/03_retry_downloads.py    # opcional, recupera fallos
python scripts/04_extract_national_zips.py
python scripts/05_load_fact_transborder.py

# 6. Cargar Border Crossing (datos públicos BTS, vía API)
python scripts/06_load_fact_border_crossing.py

# 7. Cargar Wait Times (SANDAG SODA)
python scripts/07_load_fact_wait_time.py

# 8. Pre-bake JSONs para frontend
PREBAKE_OUT_DIR=./out/prebake python scripts/08_prebake_frontend.py
# Por defecto escribe a ./out/prebake/. Para integrar con un frontend específico:
# PREBAKE_OUT_DIR=/ruta/al/frontend/public/data/frontera python scripts/08_prebake_frontend.py
```

Tiempo total estimado de un build limpio: ~30–45 minutos (dominado por descarga de TransBorder nacional).

---

## Estructura del repo

```
frontera-tijuana-public/
├── docs/
│   ├── decisions/          # 6 ADRs
│   ├── KNOWN_GAPS.md       # Limitaciones honestas del dataset
│   ├── pipeline.md         # Arquitectura técnica del pipeline
│   └── schema.md           # ERD + descripción de tablas
├── scripts/
│   ├── 01-08_*.py          # Pipeline ETL principal (correr en orden)
│   ├── explore/            # Scripts de inspección inicial de datasets
│   └── lookups/            # Mapeos auxiliares (DISAGMOT, etc.)
├── sql/
│   └── schema/             # DDLs versionados (dimensions.sql, facts.sql)
├── pyproject.toml
├── .env.example
└── .gitignore
```

---

## Licencia

MIT. Ver [`pyproject.toml`](./pyproject.toml).
