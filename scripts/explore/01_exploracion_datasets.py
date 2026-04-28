"""Phase 1: Exploratory inspection of border-corridor datasets.

Inspects three samples without writing to any database or building schema:
  1. BTS Border Crossing Entry Data (keg4-3bc2) — full national CSV
  2. Port and Commodity Trade Data via SANDAG mirror (k3a4-5ygm)
  3. SANDAG Average Daily Border Waiting Time (5tga-nezt)
"""

import json
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data_samples"
REPORTS = Path(__file__).resolve().parents[1] / "reports"
REPORTS.mkdir(exist_ok=True)

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 220)


def section(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def show_basics(df: pd.DataFrame, label: str) -> dict:
    info = {
        "label": label,
        "shape": df.shape,
        "dtypes": df.dtypes.astype(str).to_dict(),
        "null_pct": (df.isna().mean() * 100).round(2).to_dict(),
    }
    print(f"shape: {df.shape}")
    print("\ndtypes + null %:")
    for col in df.columns:
        print(f"  {col:35s} {str(df[col].dtype):15s}  nulls={info['null_pct'][col]:>6.2f}%  ex={df[col].dropna().iloc[0] if df[col].notna().any() else 'ALL NULL'!r:.80s}")
    return info


# ----------------------------------------------------------------------
# 1) BTS Border Crossing
# ----------------------------------------------------------------------
section("1. BTS Border Crossing Entry Data (keg4-3bc2) — full national CSV")
bc = pd.read_csv(DATA / "border_crossing_full.csv")
bc_info = show_basics(bc, "border_crossing")

bc["Date_parsed"] = pd.to_datetime(bc["Date"], errors="coerce")
print(f"\nDate range: {bc['Date_parsed'].min()} → {bc['Date_parsed'].max()}")
print(f"Borders: {bc['Border'].unique().tolist()}")
print(f"States: {sorted(bc['State'].dropna().unique().tolist())}")

print("\nMeasure (modes) - all unique values with counts:")
print(bc["Measure"].value_counts().to_string())

print("\nPort Name distinct count:", bc["Port Name"].nunique())

mx = bc[bc["Border"] == "US-Mexico Border"]
print(f"\nUS-Mexico ports ({mx['Port Name'].nunique()}):")
for p in sorted(mx["Port Name"].unique()):
    pd_dates = mx[mx["Port Name"] == p]["Date_parsed"]
    measures = mx[mx["Port Name"] == p]["Measure"].unique()
    print(f"  {p:30s} state={mx[mx['Port Name']==p]['State'].iloc[0]:4s}  "
          f"first={pd_dates.min().strftime('%Y-%m')}  last={pd_dates.max().strftime('%Y-%m')}  "
          f"n_measures={len(measures)}")

print("\nFOCUS — San Ysidro / Otay Mesa / Tecate deep-dive:")
for port in ["San Ysidro", "Otay Mesa", "Tecate"]:
    sub = bc[bc["Port Name"] == port]
    if sub.empty:
        print(f"  {port}: NOT FOUND")
        continue
    print(f"\n  {port} ({len(sub)} rows):")
    for m in sorted(sub["Measure"].unique()):
        m_sub = sub[sub["Measure"] == m]
        print(f"    {m:30s} first={m_sub['Date_parsed'].min().strftime('%Y-%m')} "
              f"last={m_sub['Date_parsed'].max().strftime('%Y-%m')}  n_obs={len(m_sub)}")

print("\nhead(10):")
print(bc.head(10).to_string())

# ----------------------------------------------------------------------
# 2) TransBorder Port and Commodity (SANDAG mirror k3a4-5ygm)
# ----------------------------------------------------------------------
section("2. Port and Commodity Trade Data — SANDAG mirror (k3a4-5ygm)")
tb = pd.read_csv(DATA / "transborder_sandag_sample.csv")
tb_info = show_basics(tb, "transborder_sandag")

if "month_year" in tb.columns:
    tb["month_year_p"] = pd.to_datetime(tb["month_year"], errors="coerce")
    print(f"\nmonth_year range (sample): {tb['month_year_p'].min()} → {tb['month_year_p'].max()}")

print(f"\nyear unique values (sample): {sorted(tb['year'].dropna().unique())}")
print(f"port_name unique: {sorted(tb['port_name'].dropna().unique().tolist())}")
print(f"port_code unique: {sorted(tb['port_code'].dropna().unique().tolist())}")
print(f"trade_type unique: {tb['trade_type'].unique().tolist()}")
print(f"mode_of_transportation unique: {tb['mode_of_transportation'].unique().tolist()}")
print(f"country unique: {tb['country'].unique().tolist()}")
print(f"production_location unique sample: {tb['production_location'].dropna().unique()[:10].tolist()}")
print(f"container unique: {tb['container'].unique().tolist()}")

print("\nCOMMODITY classification — sample of 20 unique values + length stats:")
commod = tb["commodity"].dropna().unique()
print(f"  n_unique in sample: {len(commod)}")
print(f"  examples (first 20):")
for c in sorted(commod)[:20]:
    print(f"    {c}")

# Try to detect HS hierarchy (codes vs descriptions)
print("\n  Length distribution of 'commodity' values (suggests classification scheme):")
lens = pd.Series([len(str(c)) for c in commod])
print(lens.describe().to_string())

print("\nhead(10):")
print(tb.head(10).to_string())

# ----------------------------------------------------------------------
# 3) SANDAG Average Daily Border Waiting Time (5tga-nezt)
# ----------------------------------------------------------------------
section("3. SANDAG Average Daily Border Waiting Time (5tga-nezt)")
wt = pd.read_csv(DATA / "sandag_wait_times_sample.csv")
wt_info = show_basics(wt, "wait_times")

wt["date_p"] = pd.to_datetime(wt["date"], errors="coerce")
print(f"\nDate range (sample, ordered DESC so this is recent slice): {wt['date_p'].min()} → {wt['date_p'].max()}")
print(f"port_name unique: {sorted(wt['port_name'].dropna().unique().tolist())}")
print(f"type (lane) unique: {sorted(wt['type'].dropna().unique().tolist())}")

print("\nCount by port × lane in this 5000-row sample:")
print(wt.groupby(["port_name", "type"]).size().to_string())

print("\nwaiting_ave stats:")
print(wt["waiting_ave"].describe().to_string())

print("\nhead(10):")
print(wt.head(10).to_string())

# ----------------------------------------------------------------------
# Save brief summary as JSON for downstream review
# ----------------------------------------------------------------------
summary = {
    "border_crossing": {
        "rows": int(bc.shape[0]),
        "cols": int(bc.shape[1]),
        "date_min": str(bc["Date_parsed"].min()),
        "date_max": str(bc["Date_parsed"].max()),
        "n_ports_total": int(bc["Port Name"].nunique()),
        "n_ports_mx": int(mx["Port Name"].nunique()),
        "measures": bc["Measure"].unique().tolist(),
    },
    "transborder_sandag_sample": {
        "rows": int(tb.shape[0]),
        "cols": int(tb.shape[1]),
        "ports": sorted(tb["port_name"].dropna().unique().tolist()),
        "modes": tb["mode_of_transportation"].unique().tolist(),
        "trade_types": tb["trade_type"].unique().tolist(),
        "n_unique_commodities_in_sample": int(tb["commodity"].nunique()),
    },
    "wait_times_sample": {
        "rows": int(wt.shape[0]),
        "cols": int(wt.shape[1]),
        "date_min": str(wt["date_p"].min()),
        "date_max": str(wt["date_p"].max()),
        "ports": sorted(wt["port_name"].dropna().unique().tolist()),
        "lane_types": sorted(wt["type"].dropna().unique().tolist()),
    },
}
(REPORTS / "phase1_summary.json").write_text(json.dumps(summary, indent=2, default=str))
print(f"\n\nSaved summary JSON → {REPORTS / 'phase1_summary.json'}")
