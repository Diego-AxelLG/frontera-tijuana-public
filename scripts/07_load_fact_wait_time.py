"""Sub-fase 4B.2 — carga de fact_wait_time desde SANDAG SODA k3a4-effn (5tga-nezt)."""

import io
import os
import sys
import time
from pathlib import Path

import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
PG = dict(host=os.environ["PG_HOST"], port=int(os.environ["PG_PORT"]),
          user=os.environ["PG_USER"], password=os.environ["PG_PASSWORD"],
          dbname=os.environ["PG_DATABASE"])

SODA_URL = "https://opendata.sandag.org/resource/5tga-nezt.json"

# port_name SANDAG → port_canonical_name (matchea con dim_port)
WAIT_PORT_TO_CANONICAL = {
    "Calexico_East": "Calexico East",
    "Calexico_West": "Calexico",
    "Otay Mesa": "Otay Mesa",
    "San Ysidro": "San Ysidro",
    "Tecate": "Tecate",
    "Andrade": "Andrade",
}

# type SANDAG → lane_canonical_name (matchea con dim_lane_type)
WAIT_LANE_TO_CANONICAL = {
    "Commercial_Fast_Lane":                  "Commercial Fast",
    "Commercial_Standard_Lane":              "Commercial Standard",
    "Passenger_vehicle_Standard_Lane":       "POV Standard",
    "Passenger_vehicle_NEXUS_SENTRI_Lane":   "POV NEXUS/SENTRI",
    "Pedestrian_standard_Lane":              "Pedestrian Standard",
    "Pedestrian_Ready_Lanes":                "Pedestrian Ready",
}


def log(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_all() -> pd.DataFrame:
    rows = []
    offset = 0
    while True:
        r = requests.get(SODA_URL, params={"$limit": 50000, "$offset": offset, "$order": "date"}, timeout=120)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < 50000:
            break
    return pd.DataFrame(rows)


def main():
    started = time.time()
    log(f"Connect → {PG['host']}/{PG['dbname']}")

    log("Fetching SANDAG 5tga-nezt vía SODA…")
    df = fetch_all()
    log(f"  raw API: {len(df):,} filas")

    # Cast date
    df["date_p"] = pd.to_datetime(df["date"], errors="coerce")
    n_bad = df["date_p"].isna().sum()
    if n_bad > 0:
        log(f"  ⚠ {n_bad} filas con date inválido — descartadas")
    df = df[df["date_p"].notna()].copy()
    df["date_id"] = df["date_p"].dt.year * 10000 + df["date_p"].dt.month * 100 + df["date_p"].dt.day

    # Cast measure
    df["waiting_avg_minutes"] = df["waiting_ave"].astype(float).round(2)

    # Lookups
    with psycopg2.connect(**PG) as c:
        with c.cursor() as cur:
            cur.execute("SELECT port_canonical_name, port_id FROM frontera.dim_port")
            port_name_to_id = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT lane_canonical_name, lane_type_id FROM frontera.dim_lane_type")
            lane_name_to_id = {r[0]: int(r[1]) for r in cur.fetchall()}

    # Mapping
    df["port_canonical"] = df["port_name"].map(WAIT_PORT_TO_CANONICAL)
    df["port_id"] = df["port_canonical"].map(port_name_to_id)
    df["lane_canonical"] = df["type"].map(WAIT_LANE_TO_CANONICAL)
    df["lane_type_id"] = df["lane_canonical"].map(lane_name_to_id)

    # Reportar fallos
    bad_port = df["port_id"].isna()
    bad_lane = df["lane_type_id"].isna()
    if bad_port.any():
        log(f"  ⚠ {bad_port.sum()} filas port_name no mapeable: {df[bad_port]['port_name'].value_counts().head().to_dict()}")
    if bad_lane.any():
        log(f"  ⚠ {bad_lane.sum()} filas type no mapeable: {df[bad_lane]['type'].value_counts().head().to_dict()}")
    df = df[~(bad_port | bad_lane)].copy()
    log(f"  tras mapping: {len(df):,}")

    # PK dupe check
    pk = ["port_id", "lane_type_id", "date_id"]
    dupes = df[df.duplicated(subset=pk, keep=False)]
    if len(dupes) > 0:
        log(f"  ⚠ {len(dupes)} dupes de PK — agregando con avg")
        log(f"    ej: {dupes.groupby(pk).size().sort_values(ascending=False).head(3).to_dict()}")
        df = df.groupby(pk, as_index=False).agg(waiting_avg_minutes=("waiting_avg_minutes", "mean"))
    else:
        log("  0 dupes de PK ✓")

    # COPY
    cols = ["port_id", "lane_type_id", "date_id", "waiting_avg_minutes"]
    df_copy = df[cols].copy()
    for c_ in ["port_id", "lane_type_id", "date_id"]:
        df_copy[c_] = df_copy[c_].astype(int)
    df_copy["waiting_avg_minutes"] = df_copy["waiting_avg_minutes"].astype(float).round(2)

    log(f"  COPY {len(df_copy):,} filas…")
    buf = io.StringIO()
    df_copy.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with psycopg2.connect(**PG) as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE frontera.fact_wait_time;")
            cur.copy_expert(
                f"COPY frontera.fact_wait_time ({','.join(cols)}) FROM STDIN WITH (FORMAT csv, DELIMITER ',', NULL '\\N')",
                buf,
            )
        c.commit()
    log(f"  inserted: {len(df_copy):,}")
    log(f"FIN. {time.time()-started:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
