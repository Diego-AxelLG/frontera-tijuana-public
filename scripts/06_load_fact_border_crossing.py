"""Sub-fase 4B.1 — carga de fact_border_crossing desde BTS keg4-3bc2."""

import io
import os
import sys
import time
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
PG = dict(host=os.environ["PG_HOST"], port=int(os.environ["PG_PORT"]),
          user=os.environ["PG_USER"], password=os.environ["PG_PASSWORD"],
          dbname=os.environ["PG_DATABASE"])

CSV = ROOT / "data_samples" / "border_crossing_full.csv"

# Border Crossing `Measure` → mode_canonical_name (matchea con dim_mode)
BC_MEASURE_TO_CANONICAL = {
    "Trucks":                       "Truck",
    "Trains":                       "Rail",                # mode_id=15 (verdadero, post-rename)
    "Personal Vehicles":            "Personal Vehicle",
    "Personal Vehicle Passengers":  "Personal Vehicle Passenger",
    "Pedestrians":                  "Pedestrian",
    "Buses":                        "Bus",
    "Bus Passengers":               "Bus Passenger",
    "Train Passengers":             "Train Passenger",
}


def log(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    started = time.time()
    log(f"Connect → {PG['host']}/{PG['dbname']}")

    log("Read CSV…")
    df = pd.read_csv(CSV)
    n0 = len(df)
    log(f"  raw: {n0:,} filas")

    # Filtros
    df = df[df["Border"] == "US-Mexico Border"].copy()
    log(f"  tras filtro US-Mexico: {len(df):,}")
    df = df[df["Port Name"] != "Cross Border Xpress"].copy()
    log(f"  tras excluir CBX: {len(df):,}")

    # Parseo de fecha
    df["date_parsed"] = pd.to_datetime(df["Date"], format="%b %Y", errors="coerce")
    n_bad_date = df["date_parsed"].isna().sum()
    if n_bad_date > 0:
        log(f"  ⚠ {n_bad_date} filas con parseo de fecha fallido — descartadas")
        log(f"    ejemplos: {df[df['date_parsed'].isna()]['Date'].head(5).tolist()}")
    df = df[df["date_parsed"].notna()].copy()
    df["date_id"] = df["date_parsed"].dt.year * 10000 + df["date_parsed"].dt.month * 100 + 1

    # Lookups
    with psycopg2.connect(**PG) as c:
        with c.cursor() as cur:
            cur.execute("SELECT port_canonical_name, port_id FROM frontera.dim_port")
            port_name_to_id = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT mode_canonical_name, mode_id FROM frontera.dim_mode")
            mode_name_to_id = {r[0]: int(r[1]) for r in cur.fetchall()}

    # Mapping
    df["port_id"] = df["Port Name"].map(port_name_to_id)
    df["mode_canonical"] = df["Measure"].map(BC_MEASURE_TO_CANONICAL)
    df["mode_id"] = df["mode_canonical"].map(mode_name_to_id)

    # Reportar/descartar fallidos
    bad_port = df["port_id"].isna()
    bad_mode = df["mode_id"].isna()
    if bad_port.any():
        log(f"  ⚠ {bad_port.sum()} filas con Port Name no mapeable: {df[bad_port]['Port Name'].value_counts().head().to_dict()}")
    if bad_mode.any():
        log(f"  ⚠ {bad_mode.sum()} filas con Measure no mapeable: {df[bad_mode]['Measure'].value_counts().head().to_dict()}")
    df = df[~(bad_port | bad_mode)].copy()
    log(f"  tras mapping: {len(df):,}")

    # Renombrar measure
    df = df.rename(columns={"Value": "crossing_count"})

    # Verificar duplicados de PK
    pk = ["port_id", "mode_id", "date_id"]
    dupes = df[df.duplicated(subset=pk, keep=False)]
    if len(dupes) > 0:
        log(f"  ⚠ {len(dupes)} filas con PK duplicada — agregando con sum")
        log(f"    top dupes: {dupes.groupby(pk).size().sort_values(ascending=False).head(3).to_dict()}")
        df = df.groupby(pk, as_index=False).agg(crossing_count=("crossing_count", "sum"))
    else:
        log("  0 dupes de PK ✓")

    # COPY
    cols = ["port_id", "mode_id", "date_id", "crossing_count"]
    df_copy = df[cols].copy()
    for c_ in ["port_id", "mode_id", "date_id"]:
        df_copy[c_] = df_copy[c_].astype(int)
    df_copy["crossing_count"] = df_copy["crossing_count"].astype("int64")

    log(f"  COPY {len(df_copy):,} filas…")
    buf = io.StringIO()
    df_copy.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with psycopg2.connect(**PG) as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE frontera.fact_border_crossing;")
            cur.copy_expert(
                f"COPY frontera.fact_border_crossing ({','.join(cols)}) FROM STDIN WITH (FORMAT csv, DELIMITER ',', NULL '\\N')",
                buf,
            )
        c.commit()
    log(f"  inserted: {len(df_copy):,}")

    log(f"FIN. {time.time()-started:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
