-- frontera-tijuana — Schema dimensional (Kimball)
-- 5 dimensiones: dim_port, dim_mode, dim_commodity_hs2, dim_date, dim_lane_type
-- Convenciones de naming y design notes en docs/pipeline.md

-- =====================================================================
-- Frontera Tijuana–San Diego: schema dimensional (Postgres)
-- Fase 2 — DDL propuesto. NO EJECUTAR sin validación previa.
--
-- Convención de nombres:
--   - snake_case en todo
--   - Singular en nombres de tabla (dim_port, fact_border_crossing).
--     Justificación: cada fila = una instancia (un puerto, un cruce mensual).
--     Es la convención Kimball estándar y la que asumen herramientas BI modernas.
--   - Schema dedicado `frontera` (no `public`).
--     Justificación: aislamiento, permisos granulares, evita colisión con
--     extensiones que dejan objetos en `public`.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS frontera;
SET search_path TO frontera, public;

-- =====================================================================
-- DIMENSIONES
-- =====================================================================

-- ---------------------------------------------------------------------
-- dim_port
-- ---------------------------------------------------------------------
CREATE TABLE dim_port (
    port_id              SMALLINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    port_canonical_name  VARCHAR(50)    NOT NULL UNIQUE,
    port_code            INTEGER        NOT NULL UNIQUE,
    country              VARCHAR(10)    NOT NULL
                                          CHECK (country IN ('Mexico', 'Canada')),
    border               VARCHAR(10)    NOT NULL
                                          CHECK (border IN ('US-Mexico', 'US-Canada')),
    state                VARCHAR(20)    NOT NULL,
    latitude             NUMERIC(9, 6),
    longitude            NUMERIC(9, 6),
    is_in_corridor       BOOLEAN        NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE  dim_port IS
    'Catálogo canónico de puertos de entrada terrestres EE.UU. ETL normaliza variantes: '
    '"Otay Mesa Station" / "Otay Mesa" → "Otay Mesa"; '
    '"Calexico-East" / "Calexico_East" → "Calexico East". '
    'CBX (Cross Border Xpress) está EXCLUIDO del scope (filtrar en ETL).';
COMMENT ON COLUMN dim_port.port_code IS
    'Código BTS oficial (4 dígitos para puertos US-Mexico, ej. 2504=San Ysidro). '
    'Único; sirve de pivote para imputar port_name NULL en TransBorder (R5 fase 1).';
COMMENT ON COLUMN dim_port.is_in_corridor IS
    'TRUE para San Ysidro, Otay Mesa, Tecate. Útil para el filtro principal del dashboard.';

-- ---------------------------------------------------------------------
-- dim_mode
-- ---------------------------------------------------------------------
CREATE TABLE dim_mode (
    mode_id              SMALLINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode_canonical_name  VARCHAR(40)    NOT NULL UNIQUE,
    source_dataset       VARCHAR(20)    NOT NULL
                                          CHECK (source_dataset IN
                                                 ('border_crossing', 'transborder', 'both'))
);

COMMENT ON TABLE  dim_mode IS
    'Catálogo de modos de transporte. Canoniza casos donde dos datasets nombran '
    'lo mismo distinto: "Trucks" (BC) + "Truck" (TB) → un solo "Truck" con '
    'source_dataset = both; "Trains" (BC) + "Rail" (TB) → "Rail" both.';
COMMENT ON COLUMN dim_mode.source_dataset IS
    'En qué dataset(s) aparece este modo. "both" = el mismo concepto físico cubierto '
    'por ambos, aunque con métricas distintas (BC cuenta cruces, TB mide flujo USD).';

-- ---------------------------------------------------------------------
-- dim_commodity_hs2
-- ---------------------------------------------------------------------
CREATE TABLE dim_commodity_hs2 (
    commodity_id           SMALLINT     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hs2_code               SMALLINT     NOT NULL UNIQUE
                                           CHECK (hs2_code BETWEEN 1 AND 99),
    hs2_description_short  VARCHAR(80)  NOT NULL,
    hs2_description_full   TEXT         NOT NULL,
    sector_grouping        VARCHAR(30)  NOT NULL
);

COMMENT ON TABLE  dim_commodity_hs2 IS
    'Capítulos del Harmonized System a 2 dígitos (HS-2). 97-99 capítulos en universo. '
    'TransBorder publica solo descripción larga; ETL deriva hs2_code mediante mapping fijo.';
COMMENT ON COLUMN dim_commodity_hs2.hs2_description_full IS
    'Texto literal del dataset (ej. "Optical, photographic, ... measuring instruments..."). '
    'Conservar para auditoría y trazabilidad.';
COMMENT ON COLUMN dim_commodity_hs2.hs2_description_short IS
    'Versión legible 1-3 palabras para tooltips/ejes (ej. "Instrumentos ópticos").';
COMMENT ON COLUMN dim_commodity_hs2.sector_grouping IS
    'Agrupador de alto nivel para visualización: Automotriz, Electrónica, Médico, '
    'Agro, Energía, Textil, Otros. Mapping HS-2 → sector definido en ETL.';

-- ---------------------------------------------------------------------
-- dim_date
-- ---------------------------------------------------------------------
CREATE TABLE dim_date (
    date_id            INTEGER      PRIMARY KEY,             -- formato yyyymmdd
    full_date          DATE         NOT NULL UNIQUE,
    year               SMALLINT     NOT NULL,
    month              SMALLINT     NOT NULL CHECK (month BETWEEN 1 AND 12),
    month_name         VARCHAR(10)  NOT NULL,
    day                SMALLINT     NOT NULL CHECK (day BETWEEN 1 AND 31),
    day_of_week        SMALLINT     NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    day_of_week_name   VARCHAR(10)  NOT NULL,
    week_of_year       SMALLINT     NOT NULL CHECK (week_of_year BETWEEN 1 AND 53),
    year_month         CHAR(7)      NOT NULL                 -- "YYYY-MM"
);

COMMENT ON TABLE dim_date IS
    'Calendario denso 1996-01-01 → fecha actual. PK como yyyymmdd entero (legible '
    'en queries: WHERE date_id = 20260423). Para tablas mensuales, FK apunta al '
    'día 1 del mes.';
COMMENT ON COLUMN dim_date.day_of_week IS '1=lunes ISO, 7=domingo.';

-- Seed (correrlo manualmente después de CREATE; se incluye aquí solo como referencia):
-- INSERT INTO dim_date (date_id, full_date, year, month, month_name, day,
--                       day_of_week, day_of_week_name, week_of_year, year_month)
-- SELECT
--     EXTRACT(YEAR FROM d)::int * 10000
--       + EXTRACT(MONTH FROM d)::int * 100
--       + EXTRACT(DAY FROM d)::int                       AS date_id,
--     d::date                                            AS full_date,
--     EXTRACT(YEAR FROM d)::smallint,
--     EXTRACT(MONTH FROM d)::smallint,
--     TO_CHAR(d, 'FMMonth'),
--     EXTRACT(DAY FROM d)::smallint,
--     EXTRACT(ISODOW FROM d)::smallint,
--     TO_CHAR(d, 'FMDay'),
--     EXTRACT(WEEK FROM d)::smallint,
--     TO_CHAR(d, 'YYYY-MM')
-- FROM generate_series('1996-01-01'::date, CURRENT_DATE, '1 day'::interval) d;

-- ---------------------------------------------------------------------
-- dim_lane_type (solo fact_wait_time)
-- ---------------------------------------------------------------------
CREATE TABLE dim_lane_type (
    lane_type_id          SMALLINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lane_canonical_name   VARCHAR(40)   NOT NULL UNIQUE,
    vehicle_category      VARCHAR(15)   NOT NULL
                                          CHECK (vehicle_category IN
                                                 ('Commercial', 'POV', 'Pedestrian')),
    is_trusted_traveler   BOOLEAN       NOT NULL
);

COMMENT ON TABLE  dim_lane_type IS
    'Tipos de carril CBP. ETL limpia los nombres del dataset SANDAG: '
    '"Commercial_Fast_Lane" → "Commercial Fast", "Passenger_vehicle_NEXUS_SENTRI_Lane" → '
    '"POV NEXUS/SENTRI", etc.';
COMMENT ON COLUMN dim_lane_type.is_trusted_traveler IS
    'TRUE para SENTRI, NEXUS, Ready Lanes y Commercial Fast (FAST). Útil para análisis '
    'de programas de viajero confiable vs estándar.';

