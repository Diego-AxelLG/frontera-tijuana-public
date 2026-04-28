"""Sub-fase 4A.3.2 + 4A.3.3 — carga de fact_transborder.

- Parte 1: carga nacional desde dot3_*.csv extraídos (data_source='bts_national')
- Parte 2: adapter SANDAG para Dec 2024 + Q1 2025 (data_source='sandag_mirror')

Idempotente vía TRUNCATE de fact_transborder al inicio.
Carga vía COPY (rápida).
"""

import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EXTRACTED = ROOT / "data_raw" / "transborder_national" / "extracted"
load_dotenv(ROOT / ".env")

PG = dict(
    host=os.environ["PG_HOST"],
    port=int(os.environ["PG_PORT"]),
    user=os.environ["PG_USER"],
    password=os.environ["PG_PASSWORD"],
    dbname=os.environ["PG_DATABASE"],
)


# =====================================================================
# Lookups BTS (DISAGMOT, TRDTYPE, CONTCODE, DF)
# =====================================================================

# DISAGMOT: invertido del mapping del spec (Truck→5, Rail→2, Air→3, ...)
# Más DISAGMOT 6 que aparece en datos como 'Other' (no documentado oficialmente)
DISAGMOT_TO_MODE_NAME = {
    # Verificado contra docs oficiales BTS (Apr 2026):
    # https://www.bts.gov/sites/bts.dot.gov/files/docs/browse-statistical-products-and-data/transborder-freight-data/220171/all-codes-north-american-transborder-freight-raw-data.pdf
    # DISAGMOT 2 NO está en docs oficiales pero 186 filas lo tienen → bucket de investigación.
    1: "Vessel",
    2: "Rail (DISAGMOT 2 - investigación pendiente)",
    3: "Air",
    4: "Mail",
    5: "Truck",
    6: "Rail",                  # CORREGIDO: antes "Other (DISAGMOT 6)" — es el verdadero Rail
    7: "Pipeline",
    8: "Other",
    9: "Foreign Trade Zone",
}

TRDTYPE_TO_DIRECTION = {1: "Export", 2: "Import"}

CONTCODE_TO_VALUE = {
    # Corrección al spec: los códigos reales en los CSVs son 0/1/X (no X/Y/Z).
    # Verificado: CONTCODE='0' → 910k filas, '1' → 238k, 'X' → 44k.
    # 0/1 = real distinción para Truck/Rail; X = N/A para modos donde no aplica
    # (Air, Pipeline, FTZ).
    "0": "Not Containerized",
    "1": "Containerized",
    "X": "Unknown",
}

DF_TO_PRODUCTION_LOCATION = {
    1.0: "Domestically Produced Goods",
    2.0: "Foreign Produced Goods",
}

# Mapping inverso (canonical mode name → DISAGMOT) — para SANDAG adapter
MODE_NAME_TO_DISAGMOT = {
    "Truck": 5, "Rail": 2, "Air": 3, "Vessel": 1, "Pipeline": 7,
    "Foreign Trade Zones (FTZs)": 9, "Mail (U.S. Postal Service)": 4, "Other": 8,
}

# SANDAG mode_of_transportation text → canonical mode_canonical_name de dim_mode
SANDAG_MODE_TO_CANONICAL = {
    "Truck": "Truck",
    "Rail": "Rail",
    "Air": "Air",
    "Vessel": "Vessel",
    "Pipeline": "Pipeline",
    "Foreign Trade Zones (FTZs)": "Foreign Trade Zone",
    "Mail (U.S. Postal Service)": "Mail",
    "Other": "Other",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize_commodity(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def conn():
    return psycopg2.connect(**PG)


# =====================================================================
# Bootstrap — cargar dimensiones existentes y descubrir DEPEs faltantes
# =====================================================================

def load_dim_lookups(c):
    """Carga lookups desde dim_port, dim_mode, dim_commodity_hs2."""
    with c.cursor() as cur:
        cur.execute("SELECT port_code, port_id FROM frontera.dim_port")
        port_code_to_id = {int(r[0]): int(r[1]) for r in cur.fetchall()}

        cur.execute("SELECT mode_canonical_name, mode_id FROM frontera.dim_mode")
        mode_name_to_id = {r[0]: int(r[1]) for r in cur.fetchall()}

        cur.execute("SELECT hs2_code, commodity_id, hs2_description_full FROM frontera.dim_commodity_hs2")
        hs2_code_to_commodity = {}
        full_norm_to_hs2 = {}
        for hs2, cid, full in cur.fetchall():
            hs2_code_to_commodity[int(hs2)] = int(cid)
            full_norm_to_hs2[normalize_commodity(full)] = int(hs2)
    return port_code_to_id, mode_name_to_id, hs2_code_to_commodity, full_norm_to_hs2


def depe_to_port_code(depe) -> int | None:
    """Convierte DEPE string a port_code int.
    - '2504' → 2504 (puerto físico)
    - '20XX' → 2099 (sintético: district*100+99)
    - NULL/inválido → None
    """
    if depe is None or (isinstance(depe, float) and pd.isna(depe)):
        return None
    s = str(depe)
    if re.fullmatch(r"\d{4}", s):
        return int(s)
    m = re.fullmatch(r"(\d{2})XX", s, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 100 + 99
    return None


def discover_and_add_missing_ports(c, port_code_to_id: dict, depes_in_data: set) -> dict:
    """Agrega como is_aggregate=TRUE:
       - DEPEs numéricos no presentes en dim_port (district codes específicos)
       - DEPEs XX (residuales) mapeados a port_code sintético {district}99
    """
    numeric_depes = set()
    xx_depes = set()
    for d in depes_in_data:
        s = str(d)
        if re.fullmatch(r"\d{4}", s):
            numeric_depes.add(int(s))
        elif re.fullmatch(r"\d{2}XX", s, re.IGNORECASE):
            xx_depes.add(s)

    # Numéricos faltantes → district aggregates físicamente identificados
    missing_numeric = sorted(numeric_depes - set(port_code_to_id.keys()))
    log(f"  DEPEs numéricos: {len(numeric_depes)} (en dim_port: {len(numeric_depes & set(port_code_to_id.keys()))}, a insertar: {len(missing_numeric)})")

    # XX → códigos sintéticos
    xx_synthetic = sorted({int(d[:2]) * 100 + 99 for d in xx_depes})
    missing_synthetic = sorted(set(xx_synthetic) - set(port_code_to_id.keys()))
    log(f"  DEPEs 'XX': {len(xx_depes)} → códigos sintéticos {len(xx_synthetic)} ({len(missing_synthetic)} a insertar)")

    rows_to_insert = []
    state_lookup = {23: "Texas", 24: "Texas", 25: "California", 26: "Arizona"}

    for code in missing_numeric:
        district = code // 100
        rows_to_insert.append((
            f"District {district} aggregate ({code})", code, "Mexico", "US-Mexico",
            state_lookup.get(district, "Other"),
            None, None, False, True,
        ))
    for code in missing_synthetic:
        district = code // 100
        rows_to_insert.append((
            f"District {district:02d}XX residual", code, "Mexico", "US-Mexico",
            state_lookup.get(district, "Other"),
            None, None, False, True,
        ))

    if rows_to_insert:
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO frontera.dim_port "
                "(port_canonical_name, port_code, country, border, state, latitude, longitude, is_in_corridor, is_aggregate) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (port_code) DO NOTHING",
                rows_to_insert,
            )
        c.commit()
        log(f"  Insertados {len(rows_to_insert)} aggregates ({len(missing_numeric)} numéricos + {len(missing_synthetic)} XX-sintéticos)")
        with c.cursor() as cur:
            cur.execute("SELECT port_code, port_id FROM frontera.dim_port")
            port_code_to_id = {int(r[0]): int(r[1]) for r in cur.fetchall()}
    return port_code_to_id


def ensure_mode_disagmot6(c):
    """Asegura que el bucket 'Rail (DISAGMOT 2 - investigación pendiente)' exista.

    NOTA HISTÓRICA: Esta función originalmente creaba 'Other (DISAGMOT 6)'.
    Tras docs oficiales BTS (2026-04-26), confirmamos que DISAGMOT 6 = Rail (verdadero).
    El modo "Rail" se renombró desde "Other (DISAGMOT 6)" → "Rail" (mode_id=15).
    El antiguo "Rail" (mode_id=2, sin uso real porque DISAGMOT 2 no existe oficialmente)
    se renombró a 'Rail (DISAGMOT 2 - investigación pendiente)' como bucket fallback.
    """
    with c.cursor() as cur:
        cur.execute("SELECT mode_id FROM frontera.dim_mode WHERE mode_canonical_name = %s",
                    ("Rail (DISAGMOT 2 - investigación pendiente)",))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO frontera.dim_mode (mode_canonical_name, source_dataset) VALUES (%s, %s)",
                ("Rail (DISAGMOT 2 - investigación pendiente)", "transborder"),
            )
            c.commit()
            log(f"  Insertado bucket fallback 'Rail (DISAGMOT 2 - investigación pendiente)'")
        # Asegurar también que 'Rail' (verdadero, DISAGMOT 6) exista
        cur.execute("SELECT mode_id FROM frontera.dim_mode WHERE mode_canonical_name = 'Rail'")
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO frontera.dim_mode (mode_canonical_name, source_dataset) VALUES (%s, %s)",
                ("Rail", "both"),
            )
            c.commit()
            log(f"  Insertado mode 'Rail' verdadero")


# =====================================================================
# Parte 1 — Carga nacional desde dot3 CSVs
# =====================================================================

def load_national(c):
    log("\n=== Carga nacional desde dot3 CSVs ===")
    files = sorted(EXTRACTED.glob("*/dot3_*.csv"))
    log(f"  archivos dot3: {len(files)}")
    if not files:
        log("  no hay archivos extraídos. Aborto carga nacional.")
        return {"national_inserted": 0}

    log("  leyendo todos los CSVs...")
    parts = []
    for f in files:
        df_part = pd.read_csv(f, dtype={"DEPE": str, "CONTCODE": str})
        parts.append(df_part)
    df = pd.concat(parts, ignore_index=True)
    log(f"  total filas raw: {len(df):,}")

    # Filtro Mexico (COUNTRY=2010)
    n0 = len(df)
    df = df[df["COUNTRY"] == 2010].copy()
    log(f"  filtro COUNTRY=2010 (México): {n0:,} → {len(df):,} ({100*len(df)/n0:.1f}%)")

    # Asegurar que dim_mode tiene 'Other (DISAGMOT 6)'
    ensure_mode_disagmot6(c)

    # Lookups (re-leer porque agregué un modo)
    port_code_to_id, mode_name_to_id, hs2_code_to_commodity, _ = load_dim_lookups(c)

    # Descubrir DEPEs y agregar faltantes (incluyendo XX → sintéticos)
    depes_in_data = set(df["DEPE"].dropna().unique().tolist())
    port_code_to_id = discover_and_add_missing_ports(c, port_code_to_id, depes_in_data)

    # Reportar valores únicos en CONTCODE para verificar que el mapping cubre todo
    cc_counts = df["CONTCODE"].value_counts()
    log(f"  CONTCODE valores observados: {dict(cc_counts)}")
    unmapped_cc = set(cc_counts.index) - set(CONTCODE_TO_VALUE.keys())
    if unmapped_cc:
        log(f"  ⚠ CONTCODE no mapeados, default 'Unknown': {unmapped_cc}")

    # Reportar DISAGMOT
    log(f"  DISAGMOT valores observados: {dict(df['DISAGMOT'].value_counts())}")

    # Mapping (port_id incluye numéricos directos y XX→sintéticos)
    log("  mapeando códigos → IDs...")
    df["port_code_int"] = df["DEPE"].apply(depe_to_port_code)
    df["port_id"] = df["port_code_int"].map(port_code_to_id)
    df["mode_name"] = df["DISAGMOT"].map(DISAGMOT_TO_MODE_NAME)
    df["mode_id"] = df["mode_name"].map(mode_name_to_id)
    df["commodity_id"] = df["COMMODITY2"].map(hs2_code_to_commodity)
    df["direction"] = df["TRDTYPE"].map(TRDTYPE_TO_DIRECTION)
    df["container"] = df["CONTCODE"].map(CONTCODE_TO_VALUE).fillna("Unknown")
    df["production_location"] = df["DF"].map(DF_TO_PRODUCTION_LOCATION).fillna("N/A")
    df["date_id"] = df["YEAR"] * 10000 + df["MONTH"] * 100 + 1

    # Drop rows with FK failures
    n_pre = len(df)
    drop_mask = df[["port_id", "mode_id", "commodity_id", "direction"]].isna().any(axis=1)
    if drop_mask.sum() > 0:
        log(f"  ⚠ {drop_mask.sum():,} filas descartadas por mapping fallido:")
        bad = df[drop_mask]
        log(f"    DEPE no mapeable (XX o NULL): {bad['port_id'].isna().sum():,}")
        log(f"    DISAGMOT no mapeable: {bad['mode_id'].isna().sum():,}")
        log(f"    COMMODITY2 no mapeable: {bad['commodity_id'].isna().sum():,}")
        log(f"    TRDTYPE no mapeable: {bad['direction'].isna().sum():,}")
        log(f"    Top DEPEs no mapeados: {Counter(bad[bad['port_id'].isna()]['DEPE']).most_common(5)}")
    df = df[~drop_mask].copy()
    log(f"  filas tras mapping: {n_pre:,} → {len(df):,}")

    # Renombrar measures
    df = df.rename(columns={"VALUE": "value_usd", "SHIPWT": "weight_kg", "FREIGHT_CHARGES": "freight_charge_usd"})

    # weight_kg=0 en exports → NULL (es ausencia, no cero real).
    # Cast a Int64 (nullable) para no perder precisión al asignar pd.NA.
    df["weight_kg"] = df["weight_kg"].astype("Int64")
    df["freight_charge_usd"] = df["freight_charge_usd"].astype("Int64")
    mask_export_zero_wt = (df["direction"] == "Export") & (df["weight_kg"] == 0)
    df.loc[mask_export_zero_wt, "weight_kg"] = pd.NA
    log(f"  weight_kg=0 en exports → NULL: {mask_export_zero_wt.sum():,} filas afectadas")

    df["data_source"] = "bts_national"

    # Detección de duplicados de PK
    pk_cols = ["port_id", "mode_id", "commodity_id", "date_id", "direction", "container", "production_location"]
    dupes_mask = df.duplicated(subset=pk_cols, keep=False)
    n_dupes = int(dupes_mask.sum())
    if n_dupes > 0:
        log(f"\n  {n_dupes:,} filas con PK duplicada (esperado: colisiones DISAGMOT 6 vs 8 → 'Other').")
        log(f"  Top 5 grupos para auditoría:")
        grouped = df[dupes_mask].groupby(pk_cols).size().sort_values(ascending=False).head(5)
        for keys, n in grouped.items():
            log(f"    {keys}: {n} ocurrencias")
        log(f"  Agregando con sum(measures) — DISAGMOT 6 y 8 son ambos 'Other' canónico, suma es semánticamente correcta.")
        # min_count=1 para que NaN+NaN=NaN (no 0). Importante para weight_kg en exports.
        df = df.groupby(pk_cols, as_index=False, dropna=False).agg(
            value_usd=("value_usd", "sum"),
            weight_kg=("weight_kg", lambda x: x.sum(min_count=1)),
            freight_charge_usd=("freight_charge_usd", lambda x: x.sum(min_count=1)),
        )
        log(f"  filas tras dedup-aggregate: {len(df):,}")
        df["data_source"] = "bts_national"

    # COPY a Postgres
    cols = ["port_id", "mode_id", "commodity_id", "date_id", "direction", "container",
            "production_location", "value_usd", "weight_kg", "freight_charge_usd", "data_source"]
    df_to_copy = df[cols].copy()
    df_to_copy["port_id"] = df_to_copy["port_id"].astype(int)
    df_to_copy["mode_id"] = df_to_copy["mode_id"].astype(int)
    df_to_copy["commodity_id"] = df_to_copy["commodity_id"].astype(int)
    df_to_copy["date_id"] = df_to_copy["date_id"].astype(int)
    df_to_copy["value_usd"] = df_to_copy["value_usd"].astype("int64")

    log(f"  preparando COPY de {len(df_to_copy):,} filas...")
    buf = io.StringIO()
    df_to_copy.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with c.cursor() as cur:
        cur.execute("TRUNCATE frontera.fact_transborder;")
        cur.copy_expert(
            f"COPY frontera.fact_transborder ({','.join(cols)}) FROM STDIN WITH (FORMAT csv, DELIMITER ',', NULL '\\N')",
            buf,
        )
    c.commit()
    log(f"  COPY OK. {len(df_to_copy):,} filas insertadas con data_source='bts_national'.")
    return {
        "national_raw": n0,
        "national_filtered_mexico": n0 - len(df_to_copy),
        "national_inserted": len(df_to_copy),
    }


# =====================================================================
# Parte 2 — Adapter SANDAG para los 4 meses faltantes
# =====================================================================

def fetch_sandag_for_month(year: int, month: int) -> pd.DataFrame:
    rows = []
    offset = 0
    while True:
        params = {
            "$where": f"year='{year}' AND month={month}",
            "$limit": 50000,
            "$offset": offset,
        }
        r = requests.get(
            "https://opendata.sandag.org/resource/k3a4-5ygm.json",
            params=params,
            timeout=120,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < 50000:
            break
    return pd.DataFrame(rows)


def load_sandag_gap(c, port_code_to_id: dict, mode_name_to_id: dict,
                    hs2_code_to_commodity: dict, full_norm_to_hs2: dict):
    log("\n=== Adapter SANDAG: Dec 2024 + Q1 2025 ===")

    # Verificar no-overlap antes
    with c.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM frontera.fact_transborder "
            "WHERE date_id BETWEEN 20241201 AND 20250301 AND data_source='bts_national'"
        )
        overlap = cur.fetchone()[0]
    log(f"  pre-check overlap (national en 2024-12..2025-03): {overlap}")
    if overlap > 0:
        log(f"  ⚠⚠⚠ overlap detectado. Abortando adapter SANDAG.")
        return {"sandag_inserted": 0, "sandag_overlap": overlap}

    months = [(2024, 12), (2025, 1), (2025, 2), (2025, 3)]
    parts = []
    counts_per_month = {}
    for y, m in months:
        df_part = fetch_sandag_for_month(y, m)
        counts_per_month[f"{y}-{m:02d}"] = len(df_part)
        log(f"  SANDAG {y}-{m:02d}: {len(df_part):,} filas")
        parts.append(df_part)
    df = pd.concat(parts, ignore_index=True)
    log(f"  SANDAG total raw: {len(df):,} filas")

    # Cast de tipos (Int64 nullable para los nullable)
    df["value_usd"] = df["value"].astype("int64")
    df["weight_kg"] = df["shipment_weight"].astype("Int64")
    df["freight_charge_usd"] = df["freight_charge"].astype("Int64")
    df["year_int"] = df["year"].astype(int)
    df["month_int"] = df["month"].astype(int)
    df["date_id"] = df["year_int"] * 10000 + df["month_int"] * 100 + 1

    # port_code → port_id
    df["port_id"] = df["port_code"].apply(lambda x: port_code_to_id.get(int(x)) if pd.notna(x) and str(x).isdigit() else None)

    # mode_of_transportation → canonical name → mode_id
    df["mode_canonical"] = df["mode_of_transportation"].map(SANDAG_MODE_TO_CANONICAL)
    df["mode_id"] = df["mode_canonical"].map(mode_name_to_id)

    # commodity (text desc) → hs2_code → commodity_id
    df["commodity_norm"] = df["commodity"].apply(normalize_commodity)
    df["hs2_code"] = df["commodity_norm"].map(full_norm_to_hs2)
    df["commodity_id"] = df["hs2_code"].map(hs2_code_to_commodity)

    # direction
    df["direction"] = df["trade_type"]

    # container (texto directo, ya en los valores válidos)
    df["container"] = df["container"].fillna("Unknown")

    # production_location: NULL → 'N/A'
    df["production_location"] = df["production_location"].fillna("N/A")

    # weight_kg=0 en exports → NULL
    mask_zero = (df["direction"] == "Export") & (df["weight_kg"] == 0)
    df.loc[mask_zero, "weight_kg"] = pd.NA

    df["data_source"] = "sandag_mirror"

    # Drops por mapping fallido
    drop_mask = df[["port_id", "mode_id", "commodity_id", "direction"]].isna().any(axis=1)
    if drop_mask.sum() > 0:
        bad = df[drop_mask]
        log(f"  ⚠ {drop_mask.sum():,} filas SANDAG descartadas por mapping fallido:")
        log(f"    port_id NULL: {bad['port_id'].isna().sum():,}, mode_id NULL: {bad['mode_id'].isna().sum():,}, commodity_id NULL: {bad['commodity_id'].isna().sum():,}")
        if bad['port_id'].isna().sum() > 0:
            log(f"    port_codes no mapeables: {bad[bad['port_id'].isna()]['port_code'].value_counts().head(5).to_dict()}")
        if bad['commodity_id'].isna().sum() > 0:
            log(f"    commodities no mapeables: {bad[bad['commodity_id'].isna()]['commodity'].value_counts().head(5).to_dict()}")
    df = df[~drop_mask].copy()

    # Detección de duplicados PK
    pk_cols = ["port_id", "mode_id", "commodity_id", "date_id", "direction", "container", "production_location"]
    dupes = df[df.duplicated(subset=pk_cols, keep=False)]
    if len(dupes) > 0:
        log(f"  ⚠ SANDAG dupes de PK: {len(dupes):,}. Top:")
        grouped = dupes.groupby(pk_cols).size().sort_values(ascending=False).head(5)
        for keys, n in grouped.items():
            log(f"    {keys}: {n}")
        log("  Aborto SANDAG por duplicados.")
        return {"sandag_inserted": 0, "sandag_dupes": len(dupes), "sandag_per_month_raw": counts_per_month}

    cols = ["port_id", "mode_id", "commodity_id", "date_id", "direction", "container",
            "production_location", "value_usd", "weight_kg", "freight_charge_usd", "data_source"]
    df_to_copy = df[cols].copy()
    for c_ in ["port_id", "mode_id", "commodity_id", "date_id"]:
        df_to_copy[c_] = df_to_copy[c_].astype(int)
    df_to_copy["value_usd"] = df_to_copy["value_usd"].astype("int64")

    buf = io.StringIO()
    df_to_copy.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with c.cursor() as cur:
        cur.copy_expert(
            f"COPY frontera.fact_transborder ({','.join(cols)}) FROM STDIN WITH (FORMAT csv, DELIMITER ',', NULL '\\N')",
            buf,
        )
    c.commit()
    log(f"  COPY SANDAG OK. {len(df_to_copy):,} filas insertadas con data_source='sandag_mirror'.")
    return {
        "sandag_per_month_raw": counts_per_month,
        "sandag_inserted": len(df_to_copy),
    }


# =====================================================================
# Validaciones V1-V7
# =====================================================================

VALIDATIONS = [
    ("V1. Filas por data_source",
     "SELECT data_source, count(*) FROM frontera.fact_transborder GROUP BY 1"),
    ("V2. Cobertura temporal por fuente",
     "SELECT data_source, min(date_id), max(date_id) FROM frontera.fact_transborder GROUP BY 1"),
    ("V3. Cross-check Otay Mar 2024 Truck Import (esperado 2_964_387_972)",
     """SELECT sum(value_usd) FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id=p.port_id
        JOIN frontera.dim_mode m ON f.mode_id=m.mode_id
        WHERE p.port_canonical_name='Otay Mesa' AND m.mode_canonical_name='Truck'
          AND f.direction='Import' AND f.date_id=20240301 AND f.data_source='bts_national'"""),
    ("V4. Corridor TTM Apr2024-Mar2025 (Otay debe dominar)",
     """SELECT p.port_canonical_name, ROUND(sum(f.value_usd)/1e9::numeric, 2) AS billions_usd
        FROM frontera.fact_transborder f JOIN frontera.dim_port p ON f.port_id=p.port_id
        WHERE p.is_in_corridor=TRUE AND f.date_id BETWEEN 20240401 AND 20250301
        GROUP BY 1 ORDER BY 2 DESC"""),
    ("V5. Total Mexico→US 2023 (esperado ~$750-800B)",
     """SELECT ROUND(sum(value_usd)/1e9::numeric, 1) AS billions_usd
        FROM frontera.fact_transborder
        WHERE date_id BETWEEN 20230101 AND 20231201 AND data_source='bts_national'"""),
    ("V6. Top 5 puertos físicos 2023 (esperado Laredo #1, El Paso #2, Otay #3)",
     """SELECT p.port_canonical_name, ROUND(sum(f.value_usd)/1e9::numeric, 1) AS billions_usd
        FROM frontera.fact_transborder f JOIN frontera.dim_port p ON f.port_id=p.port_id
        WHERE p.is_aggregate=FALSE AND f.date_id BETWEEN 20230101 AND 20231201
          AND f.data_source='bts_national'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5"""),
    ("V7. Otay sandag_mirror 4 meses",
     """SELECT date_id, count(*) AS rows, ROUND(sum(value_usd)/1e9::numeric, 2) AS bn
        FROM frontera.fact_transborder f JOIN frontera.dim_port p ON f.port_id=p.port_id
        WHERE p.port_canonical_name='Otay Mesa' AND f.data_source='sandag_mirror'
        GROUP BY 1 ORDER BY 1"""),
]


def run_validations(c):
    log("\n" + "=" * 70 + "\nVALIDACIONES V1-V7\n" + "=" * 70)
    out = {}
    with c.cursor() as cur:
        for label, sql in VALIDATIONS:
            log(f"\n--- {label} ---")
            cur.execute(sql)
            rows = cur.fetchall()
            for row in rows:
                log(f"  {row}")
            out[label] = [tuple(r) for r in rows]
    return out


# =====================================================================
# Main
# =====================================================================

def main():
    started = time.time()
    log(f"Connect to {PG['host']}/{PG['dbname']}")
    with conn() as c:
        c.autocommit = False

        # Cargar lookups iniciales
        port_code_to_id, mode_name_to_id, hs2_code_to_commodity, full_norm_to_hs2 = load_dim_lookups(c)
        log(f"\nLookups iniciales: ports={len(port_code_to_id)}, modes={len(mode_name_to_id)}, hs2={len(hs2_code_to_commodity)}")

        # Parte 1
        nat_stats = load_national(c)

        # Parte 2 — re-leer dim_port que ya tiene aggregates añadidos
        port_code_to_id, _, _, _ = load_dim_lookups(c)
        sandag_stats = load_sandag_gap(c, port_code_to_id, mode_name_to_id,
                                        hs2_code_to_commodity, full_norm_to_hs2)

        # Validaciones
        validations = run_validations(c)

    elapsed = time.time() - started
    log(f"\n=== FIN. Tiempo total: {elapsed:.1f}s ({elapsed/60:.1f} min) ===")
    summary = {"national": nat_stats, "sandag": sandag_stats, "elapsed_sec": round(elapsed, 1)}
    out_path = ROOT / "reports" / "phase4a3_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    log(f"summary -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
