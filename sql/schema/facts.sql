-- frontera-tijuana — Tablas de hechos
-- 3 facts: fact_border_crossing, fact_transborder, fact_wait_time
-- 1.29M filas totales según último prebake. Detalles en docs/pipeline.md

-- =====================================================================
-- TABLAS DE HECHOS
-- =====================================================================

-- ---------------------------------------------------------------------
-- fact_border_crossing
-- ---------------------------------------------------------------------
CREATE TABLE fact_border_crossing (
    port_id          SMALLINT     NOT NULL,
    mode_id          SMALLINT     NOT NULL,
    date_id          INTEGER      NOT NULL,
    crossing_count   BIGINT       NOT NULL CHECK (crossing_count >= 0),
    loaded_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (port_id, mode_id, date_id),
    FOREIGN KEY (port_id) REFERENCES dim_port (port_id),
    FOREIGN KEY (mode_id) REFERENCES dim_mode (mode_id),
    FOREIGN KEY (date_id) REFERENCES dim_date (date_id)
);

COMMENT ON TABLE fact_border_crossing IS
    'BTS Border Crossing Entry Data (keg4-3bc2). Granularidad mensual; date_id apunta '
    'al día 1 del mes. SOLO captura entradas a EE.UU. (US-inbound) — NO existe columna '
    '"direction" porque el dataset es unidireccional por construcción (R6 fase 1). '
    'CAVEAT R3: San Ysidro deja de reportar Trucks/Trains en 2016-07; la carga migró '
    'operativamente a Otay Mesa. No es bug del ETL.';
COMMENT ON COLUMN fact_border_crossing.crossing_count IS
    'Conteo de cruces (vehículos, peatones, pasajeros, según mode_id). BIGINT porque '
    'hay puertos-mes con >10M peatones (San Ysidro histórico).';

-- ---------------------------------------------------------------------
-- fact_transborder
-- ---------------------------------------------------------------------
CREATE TABLE fact_transborder (
    port_id              SMALLINT      NOT NULL,
    mode_id              SMALLINT      NOT NULL,
    commodity_id         SMALLINT      NOT NULL,
    date_id              INTEGER       NOT NULL,
    direction            VARCHAR(6)    NOT NULL
                                         CHECK (direction IN ('Import', 'Export')),
    container            VARCHAR(20)   NOT NULL DEFAULT 'Unknown'
                                         CHECK (container IN
                                                ('Containerized', 'Not Containerized', 'Unknown')),
    production_location  VARCHAR(30)   NOT NULL DEFAULT 'N/A'
                                         CHECK (production_location IN
                                                ('Domestically Produced Goods',
                                                 'Foreign Produced Goods',
                                                 'N/A')),
    value_usd            BIGINT        NOT NULL CHECK (value_usd >= 0),
    weight_kg            BIGINT                CHECK (weight_kg IS NULL OR weight_kg >= 0),
    freight_charge_usd   BIGINT                CHECK (freight_charge_usd IS NULL OR freight_charge_usd >= 0),
    loaded_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (port_id, mode_id, commodity_id, date_id,
                 direction, container, production_location),
    FOREIGN KEY (port_id)      REFERENCES dim_port             (port_id),
    FOREIGN KEY (mode_id)      REFERENCES dim_mode             (mode_id),
    FOREIGN KEY (commodity_id) REFERENCES dim_commodity_hs2    (commodity_id),
    FOREIGN KEY (date_id)      REFERENCES dim_date             (date_id)
);

COMMENT ON TABLE fact_transborder IS
    'Port and Commodity Trade Data (k3a4-5ygm, BTS via SANDAG). Granularidad mensual; '
    'flujo comercial México↔EE.UU. en USD y kg, desglosado por puerto × modo × HS-2 × '
    'dirección × tipo de container × origen de producción. La PK incluye container y '
    'production_location porque el dataset desdobla filas por estos atributos '
    '(observado en sample: misma combinación puerto/modo/commodity/mes con valores '
    'distintos para Containerized vs Not Containerized).';
COMMENT ON COLUMN fact_transborder.weight_kg IS
    'Peso de envío. NULLable: BTS reporta peso preferentemente para Imports; '
    'Exports puede venir vacío en algunos modos/períodos.';
COMMENT ON COLUMN fact_transborder.production_location IS
    'Solo aplica a Exports (Domestically Produced Goods vs Foreign Produced Goods). '
    'Para Imports el valor canónico es "N/A" (sentinela; PK no admite NULL).';
COMMENT ON COLUMN fact_transborder.container IS
    '"Unknown" es valor real del dataset, no inventado. Equivale a "no clasificado".';

-- ---------------------------------------------------------------------
-- fact_wait_time
-- ---------------------------------------------------------------------
CREATE TABLE fact_wait_time (
    port_id               SMALLINT       NOT NULL,
    lane_type_id          SMALLINT       NOT NULL,
    date_id               INTEGER        NOT NULL,
    waiting_avg_minutes   NUMERIC(6, 2)  NOT NULL CHECK (waiting_avg_minutes >= 0),
    loaded_at             TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (port_id, lane_type_id, date_id),
    FOREIGN KEY (port_id)      REFERENCES dim_port      (port_id),
    FOREIGN KEY (lane_type_id) REFERENCES dim_lane_type (lane_type_id),
    FOREIGN KEY (date_id)      REFERENCES dim_date      (date_id)
);

COMMENT ON TABLE fact_wait_time IS
    'SANDAG/CBP Average Daily Border Waiting Time (5tga-nezt). Granularidad DIARIA. '
    'Cobertura: 2023-01-29 → presente. Espera para entrar a EE.UU. (sentido sur→norte); '
    'el dataset no tiene la dirección norte→sur. NUMERIC(6,2) admite hasta 9999.99 min, '
    'cómodo vs el max observado (190 min).';

-- =====================================================================
-- ÍNDICES SECUNDARIOS
-- =====================================================================

-- fact_border_crossing
CREATE INDEX idx_fbc_port_date    ON fact_border_crossing (port_id, date_id);
CREATE INDEX idx_fbc_date         ON fact_border_crossing (date_id);

-- fact_transborder
CREATE INDEX idx_ftb_port_date            ON fact_transborder (port_id, date_id);
CREATE INDEX idx_ftb_date                 ON fact_transborder (date_id);
CREATE INDEX idx_ftb_commodity_direction  ON fact_transborder (commodity_id, direction);

-- fact_wait_time
CREATE INDEX idx_fwt_port_date    ON fact_wait_time (port_id, date_id);
CREATE INDEX idx_fwt_date         ON fact_wait_time (date_id);

-- dim_port: índice parcial sobre el corridor (3 filas) en lugar de índice completo
-- sobre boolean. Justificación: dim_port tendrá ~30 filas; un índice btree completo
-- sobre un boolean de baja cardinalidad no ofrece ganancia. Un índice parcial sí
-- puede acelerar EXISTS/IN cuando se filtra por is_in_corridor = TRUE en subqueries.
CREATE INDEX idx_port_corridor ON dim_port (port_id) WHERE is_in_corridor = TRUE;
