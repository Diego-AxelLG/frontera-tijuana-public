#!/usr/bin/env python3
"""Fase 5 — Pre-bake de queries y emisión de JSONs estáticos para el frontend.

Lee del schema `frontera` en Postgres y escribe 18 JSONs al directorio
configurable via env var `PREBAKE_OUT_DIR`. Default: `./out/prebake/`
relativo a la raíz del repo.

Convenciones (ver KNOWN_GAPS.md y ADR-002):
- Análisis nacional / denominadores → WHERE data_source = 'bts_national'.
- Análisis del corredor SD/Tijuana   → sin filtro adicional (incluye SANDAG mirror).
- Rankings de "puertos físicos"      → WHERE p.is_aggregate = FALSE.
- Totales nacionales                 → SIN filtro is_aggregate (los aggregates
  contienen flujos reales y omitirlos subestima -13%).
- Cortes anuales: último año con 12 meses completos para el data_source que
  alimenta la métrica.
- Series mensuales: toda la historia disponible. NULLs explícitos en gaps
  documentados (2008, 2010, 2011); NO interpolar.

Decisión sobre el "último mes completo":
- Para `bts_national`: 2024-11.
- Para corridor mixto (incluye `sandag_mirror`): 2025-03.
- Para `fact_border_crossing`: 2026-03.
- Para `fact_wait_time`: depende del run (~2026-04).

Usage:
    python etl/08_prebake_frontend.py
    python etl/08_prebake_frontend.py --out /tmp/prebake-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("PREBAKE_OUT_DIR", str(ROOT / "out" / "prebake"))
)
OUTPUT_DIR: Path = DEFAULT_OUTPUT_DIR

PG = dict(
    host=os.environ["PG_HOST"],
    port=int(os.environ.get("PG_PORT", "5432")),
    user=os.environ["PG_USER"],
    password=os.environ["PG_PASSWORD"],
    dbname=os.environ["PG_DATABASE"],
)


class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


def save(filename: str, data) -> int:
    path = OUTPUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, cls=CustomEncoder, ensure_ascii=False, indent=2)
    size = path.stat().st_size
    n = len(data) if isinstance(data, (list, dict)) else 1
    print(f"  {filename:<55} {n:>6} elem · {size/1024:>7.1f} KB")
    return size


def fetch_all(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


def fetch_one(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or {})
        return cur.fetchone()


def fetch_scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        row = cur.fetchone()
        return row[0] if row else None


def to_float(x, places=None):
    if x is None:
        return None
    v = float(x) if isinstance(x, Decimal) else float(x)
    return round(v, places) if places is not None else v


def date_id_to_iso(date_id: int) -> str:
    s = str(date_id)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def date_id_to_year_month(date_id: int) -> str:
    s = str(date_id)
    return f"{s[0:4]}-{s[4:6]}"


def shift_months(date_id: int, months: int) -> int:
    """Return new date_id (day fixed to 01) shifted by N months."""
    y, m, _ = int(str(date_id)[0:4]), int(str(date_id)[4:6]), int(str(date_id)[6:8])
    new_total = y * 12 + (m - 1) + months
    new_y = new_total // 12
    new_m = new_total % 12 + 1
    return new_y * 10000 + new_m * 100 + 1


# ---------------------------------------------------------------------------
# Helpers de cobertura
# ---------------------------------------------------------------------------

def get_last_month_id(conn, data_source: str | None = None) -> int:
    if data_source:
        sql = "SELECT MAX(date_id) FROM frontera.fact_transborder WHERE data_source = %s"
        return fetch_scalar(conn, sql, (data_source,))
    return fetch_scalar(conn, "SELECT MAX(date_id) FROM frontera.fact_transborder")


def get_last_complete_year(conn, data_source: str | None = None) -> int:
    where = "WHERE data_source = %s" if data_source else ""
    params = (data_source,) if data_source else None
    sql = f"""
        SELECT EXTRACT(YEAR FROM to_date(date_id::text, 'YYYYMMDD'))::int AS y
        FROM frontera.fact_transborder
        {where}
        GROUP BY 1
        HAVING COUNT(DISTINCT date_id) = 12
        ORDER BY 1 DESC
        LIMIT 1
    """
    return fetch_scalar(conn, sql, params)


def get_last_complete_year_bc(conn) -> int:
    sql = """
        SELECT EXTRACT(YEAR FROM to_date(date_id::text, 'YYYYMMDD'))::int AS y
        FROM frontera.fact_border_crossing
        GROUP BY 1
        HAVING COUNT(DISTINCT date_id) = 12
        ORDER BY 1 DESC
        LIMIT 1
    """
    return fetch_scalar(conn, sql)


def get_last_date_wt(conn) -> int:
    return fetch_scalar(conn, "SELECT MAX(date_id) FROM frontera.fact_wait_time")


# ---------------------------------------------------------------------------
# Section 0 — meta.json
# ---------------------------------------------------------------------------

def export_meta(conn):
    nat_min = fetch_scalar(conn, "SELECT MIN(date_id) FROM frontera.fact_transborder WHERE data_source='bts_national'")
    nat_max = fetch_scalar(conn, "SELECT MAX(date_id) FROM frontera.fact_transborder WHERE data_source='bts_national'")
    cor_min = fetch_scalar(conn, "SELECT MIN(date_id) FROM frontera.fact_transborder")
    cor_max = fetch_scalar(conn, "SELECT MAX(date_id) FROM frontera.fact_transborder")
    bc_min = fetch_scalar(conn, "SELECT MIN(date_id) FROM frontera.fact_border_crossing")
    bc_max = fetch_scalar(conn, "SELECT MAX(date_id) FROM frontera.fact_border_crossing")
    wt_min = fetch_scalar(conn, "SELECT MIN(date_id) FROM frontera.fact_wait_time")
    wt_max = fetch_scalar(conn, "SELECT MAX(date_id) FROM frontera.fact_wait_time")

    data = {
        "version": "1.0.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_coverage": {
            "transborder_national": {
                "start": date_id_to_year_month(nat_min),
                "end": date_id_to_year_month(nat_max),
                "gaps": ["2008", "2010", "2011"],
                "note": "ZIPs anuales truncados/cifrados en Wayback (G2). 2006 entero también ausente (G1)."
            },
            "transborder_corridor_mixed": {
                "start": date_id_to_year_month(cor_min),
                "end": date_id_to_year_month(cor_max),
                "note": "Incluye SANDAG mirror para Dec 2024 + Q1 2025 (solo distrito 25)."
            },
            "border_crossing": {
                "start": date_id_to_year_month(bc_min),
                "end": date_id_to_year_month(bc_max),
                "note": "San Ysidro deja de reportar Trucks en 2016-07 (G8)."
            },
            "wait_time": {
                "start": date_id_to_iso(wt_min),
                "end": date_id_to_iso(wt_max),
                "note": "Cobertura nativa SANDAG: arranca 2023-01-29 (G12)."
            }
        },
        "known_gaps_summary": "14 gaps documentados (G1-G14). Ver docs/KNOWN_GAPS.md en frontera-tijuana. Resumen: 2008/2010/2011 ausentes, 2006 ausente, Dec 2024+Q1 2025 solo distrito 25 SANDAG.",
        "last_complete_month": {
            "national": date_id_to_year_month(nat_max),
            "corridor": date_id_to_year_month(cor_max),
            "border_crossing": date_id_to_year_month(bc_max),
            "wait_time": date_id_to_iso(wt_max),
        }
    }
    save("meta.json", data)


# ---------------------------------------------------------------------------
# Section 1 — Panorama
# ---------------------------------------------------------------------------

def export_section1_kpis(conn):
    nat_max = get_last_month_id(conn, "bts_national")  # 20241101
    ttm_end = nat_max
    ttm_start = shift_months(nat_max, -11)             # 20231201
    ttm_prev_end = shift_months(nat_max, -12)          # 20231101
    ttm_prev_start = shift_months(nat_max, -23)        # 20221201

    ttm_curr = fetch_scalar(conn, """
        SELECT COALESCE(SUM(value_usd), 0)
        FROM frontera.fact_transborder
        WHERE data_source = 'bts_national'
          AND date_id BETWEEN %s AND %s
    """, (ttm_start, ttm_end))

    ttm_prev = fetch_scalar(conn, """
        SELECT COALESCE(SUM(value_usd), 0)
        FROM frontera.fact_transborder
        WHERE data_source = 'bts_national'
          AND date_id BETWEEN %s AND %s
    """, (ttm_prev_start, ttm_prev_end))

    full_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(value_usd), 0)
        FROM frontera.fact_transborder
        WHERE data_source = 'bts_national'
          AND date_id BETWEEN 20190101 AND 20191201
    """)

    delta_yoy = (ttm_curr - ttm_prev) / ttm_prev * 100 if ttm_prev else None
    delta_19 = (ttm_curr - full_2019) / full_2019 * 100 if full_2019 else None

    # corridor share (TTM ending nat_max for both, bts_national both sides)
    corridor_ttm = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE f.data_source = 'bts_national'
          AND p.is_in_corridor = TRUE
          AND f.date_id BETWEEN %s AND %s
    """, (ttm_start, ttm_end))
    corridor_share = (corridor_ttm / ttm_curr * 100) if ttm_curr else None

    # crossings corridor — last complete year (fact_border_crossing)
    last_y_bc = get_last_complete_year_bc(conn)
    crossings_corr = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.is_in_corridor = TRUE
          AND d.year = %s
    """, (last_y_bc,))

    # wait Otay Commercial Standard last 30 days
    wt_max = get_last_date_wt(conn)
    wt_min30 = (datetime.strptime(str(wt_max), "%Y%m%d").date() - timedelta(days=30))
    wt_min30_id = int(wt_min30.strftime("%Y%m%d"))
    wait_otay = fetch_scalar(conn, """
        SELECT ROUND(AVG(f.waiting_avg_minutes)::numeric, 2)
        FROM frontera.fact_wait_time f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_lane_type l ON f.lane_type_id = l.lane_type_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND l.lane_canonical_name = 'Commercial Standard'
          AND f.date_id BETWEEN %s AND %s
    """, (wt_min30_id, wt_max))

    data = {
        "freight_total_ttm_national": {
            "value_usd": int(ttm_curr or 0),
            "delta_yoy_pct": to_float(delta_yoy, 2),
            "delta_vs_2019_pct": to_float(delta_19, 2),
            "label": f"Freight Mexico→US (TTM cerrando {date_id_to_year_month(ttm_end)})",
            "ttm_window": {
                "start": date_id_to_year_month(ttm_start),
                "end": date_id_to_year_month(ttm_end),
            },
            "data_source_note": "Solo bts_national. Q1 2025 disponible para corridor (sandag_mirror) pero no para denominador nacional."
        },
        "corridor_share_national": {
            "value_pct": to_float(corridor_share, 2),
            "label": "Corridor SD/Imperial sobre nacional Mexico→US",
            "window": "TTM bts_national",
        },
        "crossings_corridor_total_yearly": {
            "value": int(crossings_corr or 0),
            "year": last_y_bc,
            "label": f"Cruces totales corridor {last_y_bc} (todos los modos, hacia US)"
        },
        "wait_otay_commercial_avg_30d": {
            "minutes": to_float(wait_otay, 2),
            "label": "Espera promedio Otay Comercial Standard (últimos 30 días)",
            "window_end": date_id_to_iso(wt_max),
        }
    }
    save("section1_panorama/kpis.json", data)


def export_section1_timeline_value(conn):
    """Serie mensual nacional bts_national con NULLs explícitos en gaps."""
    rows = fetch_all(conn, """
        SELECT date_id, SUM(value_usd) AS value_usd
        FROM frontera.fact_transborder
        WHERE data_source = 'bts_national'
        GROUP BY date_id
        ORDER BY date_id
    """)
    by_dateid = {int(r["date_id"]): int(r["value_usd"] or 0) for r in rows}

    # Generar secuencia mensual completa 2007-01 → último mes nacional, con NULL en gaps
    last_id = max(by_dateid.keys())
    last_y, last_m = int(str(last_id)[0:4]), int(str(last_id)[4:6])
    series = []
    for y in range(2007, last_y + 1):
        for m in range(1, 13):
            if y == last_y and m > last_m:
                break
            did = y * 10000 + m * 100 + 1
            iso = f"{y:04d}-{m:02d}-01"
            v = by_dateid.get(did)
            series.append({"date": iso, "value_usd": (int(v) if v is not None else None)})

    annotations = [
        {"date": "2008-01-01", "label": "Gap: año 2008 ausente (Wayback truncó ZIP anual)"},
        {"date": "2009-01-01", "label": "Recesión global / crisis financiera"},
        {"date": "2010-01-01", "label": "Gap: año 2010 ausente"},
        {"date": "2011-01-01", "label": "Gap: año 2011 ausente"},
        {"date": "2020-03-01", "label": "Inicio COVID-19"},
        {"date": "2022-01-01", "label": "Aceleración nearshoring"},
        {"date": "2024-11-01", "label": "Último mes nacional disponible (bts_national)"},
    ]

    data = {
        "series": series,
        "annotations": annotations,
        "filter": {"data_source": "bts_national", "scope": "todos los puertos US-Mexico (incluye aggregates)"},
        "unit": "USD"
    }
    save("section1_panorama/timeline_value.json", data)


def export_section1_timeline_crossings(conn):
    """Serie mensual nacional total de cruces (todos los modos US-Mexico)."""
    rows = fetch_all(conn, """
        SELECT f.date_id, SUM(f.crossing_count) AS total
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.border = 'US-Mexico'
          AND p.is_aggregate = FALSE
        GROUP BY f.date_id
        ORDER BY f.date_id
    """)
    series = [{"date": date_id_to_iso(int(r["date_id"])), "crossings": int(r["total"])} for r in rows]

    annotations = [
        {"date": "2001-09-01", "label": "9/11 — endurecimiento CBP"},
        {"date": "2008-09-01", "label": "Crisis financiera 2008"},
        {"date": "2016-07-01", "label": "San Ysidro deja de reportar Trucks (carga migra a Otay)"},
        {"date": "2020-03-01", "label": "COVID-19 — restricciones cruces no esenciales"},
        {"date": "2021-11-01", "label": "Reapertura cruces no esenciales"},
    ]
    data = {
        "series": series,
        "annotations": annotations,
        "filter": {"scope": "Todos los puertos físicos US-Mexico (is_aggregate=FALSE), todos los modos"},
        "unit": "cruces (entradas a US)"
    }
    save("section1_panorama/timeline_crossings.json", data)


def export_section1_puertos_top(conn):
    last_y = get_last_complete_year(conn, "bts_national")
    rows = fetch_all(conn, """
        SELECT p.port_canonical_name AS port,
               p.state,
               ROUND((SUM(f.value_usd) / 1e9)::numeric, 2) AS bn_usd
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.is_aggregate = FALSE
          AND f.data_source = 'bts_national'
          AND d.year = %s
        GROUP BY 1, 2
        ORDER BY 3 DESC
        LIMIT 10
    """, (last_y,))
    data = {
        "year": last_y,
        "filter": "is_aggregate=FALSE, data_source=bts_national",
        "items": [{"port": r["port"], "state": r["state"], "bn_usd": to_float(r["bn_usd"], 2)} for r in rows]
    }
    save("section1_panorama/puertos_top.json", data)


# ---------------------------------------------------------------------------
# Section 2 — Otay
# ---------------------------------------------------------------------------

def export_section2_kpis(conn):
    cor_max = get_last_month_id(conn)                  # 20250301
    ttm_end = cor_max
    ttm_start = shift_months(cor_max, -11)             # 20240401
    ttm_prev_end = shift_months(cor_max, -12)          # 20240301
    ttm_prev_start = shift_months(cor_max, -23)        # 20230401

    ttm_curr = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND f.date_id BETWEEN %s AND %s
    """, (ttm_start, ttm_end))
    ttm_prev = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND f.date_id BETWEEN %s AND %s
    """, (ttm_prev_start, ttm_prev_end))
    delta_yoy = (ttm_curr - ttm_prev) / ttm_prev * 100 if ttm_prev else None

    # share corridor (Otay vs corridor total) — usar último año completo de corridor mixto
    last_y_corr = get_last_complete_year(conn)        # 2024 (corridor incluye SANDAG dec 2024)
    otay_y = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND d.year = %s
    """, (last_y_corr,))
    corridor_y = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.is_in_corridor = TRUE AND d.year = %s
    """, (last_y_corr,))
    share_corr = (otay_y / corridor_y * 100) if corridor_y else None

    # share national — usar último año completo nacional bts_national
    last_y_nat = get_last_complete_year(conn, "bts_national")    # 2023
    otay_nat = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND f.data_source = 'bts_national'
          AND d.year = %s
    """, (last_y_nat,))
    nat_total = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE f.data_source = 'bts_national' AND d.year = %s
    """, (last_y_nat,))
    share_nat = (otay_nat / nat_total * 100) if nat_total else None

    # trucks last complete year border crossing (2025)
    last_y_bc = get_last_complete_year_bc(conn)
    trucks = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND m.mode_canonical_name = 'Truck'
          AND d.year = %s
    """, (last_y_bc,))

    # usd_per_truck — Otay 2024: value mixed (12 meses) / trucks 2024
    USD_TRUCK_YEAR = 2024
    val_2024 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND d.year = %s
    """, (USD_TRUCK_YEAR,))
    trucks_2024 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND m.mode_canonical_name = 'Truck'
          AND d.year = %s
    """, (USD_TRUCK_YEAR,))
    usd_truck = (val_2024 / trucks_2024) if trucks_2024 else None

    val_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND d.year = 2019
    """)
    trucks_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND m.mode_canonical_name='Truck' AND d.year = 2019
    """)
    usd_truck_2019 = (val_2019 / trucks_2019) if trucks_2019 else None
    usd_truck_delta_19 = (usd_truck / usd_truck_2019 - 1) * 100 if usd_truck_2019 else None

    data = {
        "value_ttm": {
            "value_usd": int(ttm_curr or 0),
            "delta_yoy_pct": to_float(delta_yoy, 2),
            "label": f"Valor TTM Otay ({date_id_to_year_month(ttm_start)} – {date_id_to_year_month(ttm_end)})",
            "ttm_window": {"start": date_id_to_year_month(ttm_start), "end": date_id_to_year_month(ttm_end)},
            "data_source_note": "Sin filtro data_source: Apr 2024-Nov 2024 = bts_national, Dec 2024-Mar 2025 = sandag_mirror."
        },
        "share_corridor_pct": {
            "value_pct": to_float(share_corr, 2),
            "year": last_y_corr,
            "label": f"Otay sobre corridor SD/Tijuana ({last_y_corr})"
        },
        "share_national_pct": {
            "value_pct": to_float(share_nat, 2),
            "year": last_y_nat,
            "label": f"Otay sobre nacional Mexico→US ({last_y_nat}, bts_national)"
        },
        "trucks_yearly": {
            "value": int(trucks or 0),
            "year": last_y_bc,
            "label": f"Camiones que cruzaron Otay {last_y_bc} (entradas a US)"
        },
        "usd_per_truck": {
            "value_usd": to_float(usd_truck, 0),
            "year": USD_TRUCK_YEAR,
            "label": f"Valor promedio por camión ({USD_TRUCK_YEAR})",
            "delta_vs_2019_pct": to_float(usd_truck_delta_19, 2),
        }
    }
    save("section2_otay/kpis.json", data)


def export_section2_value_monthly(conn):
    rows = fetch_all(conn, """
        SELECT f.date_id, f.data_source, SUM(f.value_usd) AS value_usd
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name = 'Otay Mesa'
        GROUP BY f.date_id, f.data_source
        ORDER BY f.date_id
    """)
    # Combinar (algún mes podría aparecer en ambos sources si la PK lo permitiera; en
    # nuestro modelo actual son disjuntos por construcción ETL)
    by_dateid = {}
    for r in rows:
        did = int(r["date_id"])
        by_dateid.setdefault(did, {"value_usd": 0, "data_source": r["data_source"]})
        by_dateid[did]["value_usd"] += int(r["value_usd"] or 0)

    last_id = max(by_dateid.keys())
    last_y, last_m = int(str(last_id)[0:4]), int(str(last_id)[4:6])
    series = []
    for y in range(2007, last_y + 1):
        for m in range(1, 13):
            if y == last_y and m > last_m:
                break
            did = y * 10000 + m * 100 + 1
            iso = f"{y:04d}-{m:02d}-01"
            entry = by_dateid.get(did)
            series.append({
                "date": iso,
                "value_usd": (entry["value_usd"] if entry else None),
                "data_source": (entry["data_source"] if entry else None),
            })

    save("section2_otay/value_monthly.json", {"port": "Otay Mesa", "series": series, "unit": "USD"})


def export_section2_value_yearly(conn):
    last_id = get_last_month_id(conn)
    last_y_complete = get_last_complete_year(conn)  # último año con 12 meses (corridor)

    rows = fetch_all(conn, """
        SELECT EXTRACT(YEAR FROM to_date(f.date_id::text, 'YYYYMMDD'))::int AS year,
               COUNT(DISTINCT f.date_id) AS months,
               SUM(f.value_usd) AS value_usd
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name = 'Otay Mesa'
        GROUP BY 1
        ORDER BY 1
    """)
    items = []
    for r in rows:
        items.append({
            "year": int(r["year"]),
            "bn_usd": round(int(r["value_usd"]) / 1e9, 3),
            "months_loaded": int(r["months"]),
            "complete": int(r["months"]) == 12,
        })
    save("section2_otay/value_yearly.json", {
        "port": "Otay Mesa",
        "last_complete_year": last_y_complete,
        "items": items
    })


def export_section2_mix_sectores(conn):
    last_y = get_last_complete_year(conn)  # 2024 (corridor incluye SANDAG dec)

    # current_year: Otay full year by sector
    rows_curr = fetch_all(conn, """
        SELECT c.sector_grouping AS sector,
               SUM(f.value_usd) AS value_usd
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_commodity_hs2 c ON f.commodity_id = c.commodity_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND d.year = %s
        GROUP BY 1
        ORDER BY 2 DESC
    """, (last_y,))
    total_curr = sum(int(r["value_usd"] or 0) for r in rows_curr)
    current_data = []
    for r in rows_curr:
        v = int(r["value_usd"] or 0)
        current_data.append({
            "sector": r["sector"],
            "bn_usd": round(v / 1e9, 3),
            "pct": round(v / total_curr * 100, 2) if total_curr else None,
        })

    # historic_yearly (2018-last_y): sector × year as a stacked area
    rows_hist = fetch_all(conn, """
        SELECT EXTRACT(YEAR FROM to_date(f.date_id::text, 'YYYYMMDD'))::int AS year,
               c.sector_grouping AS sector,
               SUM(f.value_usd) AS value_usd
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_commodity_hs2 c ON f.commodity_id = c.commodity_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND f.date_id BETWEEN 20180101 AND %s
        GROUP BY 1, 2
        ORDER BY 1, 2
    """, (last_y * 10000 + 1231,))

    # Pivot
    years = sorted({int(r["year"]) for r in rows_hist})
    sectors_set = sorted({r["sector"] for r in rows_hist})
    by_year = {y: {} for y in years}
    for r in rows_hist:
        by_year[int(r["year"])][r["sector"]] = int(r["value_usd"] or 0)
    sector_series = {}
    for s in sectors_set:
        sector_series[s] = []
        for y in years:
            v = by_year[y].get(s, 0)
            year_total = sum(by_year[y].values()) or 1
            sector_series[s].append({
                "bn_usd": round(v / 1e9, 3),
                "pct": round(v / year_total * 100, 2) if year_total else None,
            })

    save("section2_otay/mix_sectores.json", {
        "port": "Otay Mesa",
        "current_year": {
            "year": last_y,
            "data": current_data,
        },
        "historic_yearly": {
            "years": years,
            "sectors": sector_series,
        }
    })


def export_section2_nearshoring_ratio(conn):
    """Ratio value:trucks (sorpresa #2). Compara 2019 vs 2024 anualizado (bts_national)."""
    # value 2019 — Otay full year bts_national
    val_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND f.data_source = 'bts_national'
          AND f.date_id BETWEEN 20190101 AND 20191201
    """)
    # value 2024 Jan-Nov bts_national, anualizado × 12/11
    val_2024_11 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND f.data_source = 'bts_national'
          AND f.date_id BETWEEN 20240101 AND 20241101
    """)
    val_2019 = int(val_2019 or 0)
    val_2024_11 = int(val_2024_11 or 0)
    val_2024_anu = val_2024_11 * 12 / 11

    # trucks 2019 / 2024 (full years, fact_border_crossing)
    trk_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND m.mode_canonical_name='Truck' AND d.year = 2019
    """)
    trk_2024 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND m.mode_canonical_name='Truck' AND d.year = 2024
    """)
    trk_2019 = int(trk_2019 or 0)
    trk_2024 = int(trk_2024 or 0)
    val_growth = (val_2024_anu - val_2019) / val_2019 * 100 if val_2019 else None
    trk_growth = (trk_2024 - trk_2019) / trk_2019 * 100 if trk_2019 else None
    ratio = (val_growth / trk_growth) if trk_growth else None

    data = {
        "comparison": {
            "value_2019_bn": round(val_2019 / 1e9, 2),
            "value_2024_bn_anualizado": round(val_2024_anu / 1e9, 2),
            "value_growth_pct": to_float(val_growth, 2),
            "trucks_2019": int(trk_2019 or 0),
            "trucks_2024": int(trk_2024 or 0),
            "trucks_growth_pct": to_float(trk_growth, 2),
            "ratio_value_to_volume_growth": to_float(ratio, 2),
        },
        "narrative": (
            "El valor en USD creció ~%s× más rápido que el volumen de camiones "
            "comparando 2019 vs 2024 (anualizado). Firma estadística del nearshoring: "
            "se mueve más densidad económica por unidad de logística."
        ) % to_float(ratio, 1),
        "method_note": "value_2024 anualizado: enero-noviembre 2024 bts_national escalado ×12/11 (sin Dec 2024 sandag para mantener apples-to-apples). Trucks 2024 es año completo (fact_border_crossing cubre 2024)."
    }
    save("section2_otay/nearshoring_ratio.json", data)


def export_section2_corridor_asimetria(conn):
    last_y = get_last_complete_year(conn)
    rows = fetch_all(conn, """
        SELECT p.port_canonical_name AS port,
               SUM(f.value_usd) AS value_usd
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.is_in_corridor = TRUE AND d.year = %s
        GROUP BY 1
        ORDER BY 2 DESC
    """, (last_y,))
    total = sum(int(r["value_usd"] or 0) for r in rows)
    items = []
    otay_v = next((int(r["value_usd"]) for r in rows if r["port"] == "Otay Mesa"), 0)
    for r in rows:
        v = int(r["value_usd"] or 0)
        items.append({
            "port": r["port"],
            "bn_usd": round(v / 1e9, 3),
            "pct_corridor": round(v / total * 100, 2) if total else None,
            "ratio_otay_to_port": round(otay_v / v, 1) if v and r["port"] != "Otay Mesa" else None,
        })

    save("section2_otay/corridor_asimetria.json", {
        "comparison_year": last_y,
        "data": items,
        "headline": "El corridor no es 3 puertos. Es Otay con 2 satélites."
    })


# ---------------------------------------------------------------------------
# Section 3 — Wait
# ---------------------------------------------------------------------------

CORR_PORTS_FOR_WT = ["Otay Mesa", "San Ysidro", "Tecate"]
LANES_PRIORITY = [
    "Commercial Standard",
    "Commercial Fast",
    "POV Standard",
    "POV NEXUS/SENTRI",
    "Pedestrian Standard",
    "Pedestrian Ready",
]


def export_section3_kpis(conn):
    wt_max = get_last_date_wt(conn)
    wt_min30 = (datetime.strptime(str(wt_max), "%Y%m%d").date() - timedelta(days=30))
    wt_min30_id = int(wt_min30.strftime("%Y%m%d"))

    def avg(port, lane):
        return fetch_scalar(conn, """
            SELECT ROUND(AVG(f.waiting_avg_minutes)::numeric, 2)
            FROM frontera.fact_wait_time f
            JOIN frontera.dim_port p ON f.port_id = p.port_id
            JOIN frontera.dim_lane_type l ON f.lane_type_id = l.lane_type_id
            WHERE p.port_canonical_name = %s
              AND l.lane_canonical_name = %s
              AND f.date_id BETWEEN %s AND %s
        """, (port, lane, wt_min30_id, wt_max))

    data = {
        "otay_commercial_standard_avg_30d": to_float(avg("Otay Mesa", "Commercial Standard"), 2),
        "otay_commercial_fast_avg_30d": to_float(avg("Otay Mesa", "Commercial Fast"), 2),
        "san_ysidro_pov_standard_avg_30d": to_float(avg("San Ysidro", "POV Standard"), 2),
        "tecate_commercial_standard_avg_30d": to_float(avg("Tecate", "Commercial Standard"), 2),
        "data_freshness": date_id_to_iso(wt_max),
        "window_days": 30,
    }
    save("section3_wait/kpis.json", data)


def export_section3_heatmap(conn):
    """Matriz week_of_year × day_of_week (lun=1..dom=7 ISO) por puerto×lane."""
    rows = fetch_all(conn, """
        SELECT p.port_canonical_name AS port,
               l.lane_canonical_name AS lane,
               d.week_of_year AS woy,
               d.day_of_week AS dow,
               ROUND(AVG(f.waiting_avg_minutes)::numeric, 2) AS avg_min,
               COUNT(*) AS n
        FROM frontera.fact_wait_time f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_lane_type l ON f.lane_type_id = l.lane_type_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name IN %s
        GROUP BY 1, 2, 3, 4
    """, (tuple(CORR_PORTS_FOR_WT),))

    # Combinaciones port|lane: only those with at least one row
    combos = sorted({(r["port"], r["lane"]) for r in rows})
    by_combo = {}
    for r in rows:
        key = f"{r['port']}|{r['lane']}"
        by_combo.setdefault(key, {})
        by_combo[key].setdefault(int(r["woy"]), [None] * 7)
        # ISO dow: 1=lunes ... 7=domingo. Index 0..6 sin offset.
        by_combo[key][int(r["woy"])][int(r["dow"]) - 1] = float(r["avg_min"])

    # default view: Otay Mesa | Commercial Standard if available
    default_port, default_lane = "Otay Mesa", "Commercial Standard"
    if (default_port, default_lane) not in combos and combos:
        default_port, default_lane = combos[0]

    data = {
        "filters_available": {
            "ports": sorted({p for p, _ in combos}),
            "lanes": sorted({lane for _, lane in combos}),
        },
        "default_view": {"port": default_port, "lane": default_lane},
        "dow_order": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
        "data": {
            key: {"matrix": {str(w): vals for w, vals in sorted(woy_map.items())}}
            for key, woy_map in by_combo.items()
        },
        "method_note": "Cada celda = avg de waiting_avg_minutes para esa (semana ISO, día ISO) agregando todos los años 2023-presente."
    }
    save("section3_wait/heatmap_dow_week.json", data)


def export_section3_fast_vs_standard(conn):
    """Comparativa Fast vs Standard (Otay) último año completo de wait_time."""
    # Año más reciente con cobertura sustancial — usar 2024 (último año completo)
    YEAR = 2024
    rows = fetch_all(conn, """
        SELECT l.lane_canonical_name AS lane,
               ROUND(AVG(f.waiting_avg_minutes)::numeric, 2) AS avg_min,
               ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY f.waiting_avg_minutes)::numeric, 2) AS p50_min,
               ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY f.waiting_avg_minutes)::numeric, 2) AS p95_min,
               ROUND(MAX(f.waiting_avg_minutes)::numeric, 2) AS max_min,
               COUNT(*) AS n
        FROM frontera.fact_wait_time f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_lane_type l ON f.lane_type_id = l.lane_type_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa'
          AND l.lane_canonical_name IN ('Commercial Standard', 'Commercial Fast')
          AND d.year = %s
        GROUP BY 1
    """, (YEAR,))

    by_lane = {r["lane"]: r for r in rows}
    standard = by_lane.get("Commercial Standard")
    fast = by_lane.get("Commercial Fast")

    def pct_diff(a, b):
        return round((float(a) - float(b)) / float(b) * 100, 2) if a and b else None

    savings = None
    if standard and fast:
        savings = {
            "avg_pct": pct_diff(fast["avg_min"], standard["avg_min"]),
            "p50_pct": pct_diff(fast["p50_min"], standard["p50_min"]),
            "p95_pct": pct_diff(fast["p95_min"], standard["p95_min"]),
            "max_pct": pct_diff(fast["max_min"], standard["max_min"]),
        }

    data = {
        "port": "Otay Mesa",
        "year": YEAR,
        "lanes": [
            {
                "lane": r["lane"],
                "avg_min": to_float(r["avg_min"], 2),
                "p50_min": to_float(r["p50_min"], 2),
                "p95_min": to_float(r["p95_min"], 2),
                "max_min": to_float(r["max_min"], 2),
                "n_days": int(r["n"]),
            } for r in rows
        ],
        "savings": savings,
        "narrative": "FAST no es para todos los días — es para los días que importan: el ahorro es desproporcionado en p95 y máximos."
    }
    save("section3_wait/fast_vs_standard.json", data)


def export_section3_tendencia_anual(conn):
    """Serie mensual avg wait por (puerto, lane) corridor 2023-presente."""
    rows = fetch_all(conn, """
        SELECT p.port_canonical_name AS port,
               l.lane_canonical_name AS lane,
               d.year_month AS ym,
               ROUND(AVG(f.waiting_avg_minutes)::numeric, 2) AS avg_min,
               COUNT(*) AS n
        FROM frontera.fact_wait_time f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_lane_type l ON f.lane_type_id = l.lane_type_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name IN %s
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    """, (tuple(CORR_PORTS_FOR_WT),))

    series_by_combo = {}
    for r in rows:
        key = f"{r['port']}|{r['lane']}"
        series_by_combo.setdefault(key, []).append({
            "year_month": r["ym"],
            "avg_min": to_float(r["avg_min"], 2),
            "n_days": int(r["n"]),
        })

    save("section3_wait/tendencia_anual.json", {
        "default_view": {"port": "Otay Mesa", "lane": "Commercial Standard"},
        "data": series_by_combo,
        "method_note": "Avg mensual de waiting_avg_minutes. Combinaciones puerto×lane sin filas (e.g. Tecate Commercial Fast) son por diseño operativo CBP."
    })


# ---------------------------------------------------------------------------
# Section 4 — Recovery
# ---------------------------------------------------------------------------

def export_section4_kpis(conn):
    val_2024 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND d.year = 2024
    """)
    val_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.value_usd), 0)
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name = 'Otay Mesa' AND d.year = 2019
    """)
    trk_2024 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name='Otay Mesa' AND m.mode_canonical_name='Truck' AND d.year=2024
    """)
    trk_2019 = fetch_scalar(conn, """
        SELECT COALESCE(SUM(f.crossing_count), 0)
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        JOIN frontera.dim_date d ON f.date_id = d.date_id
        WHERE p.port_canonical_name='Otay Mesa' AND m.mode_canonical_name='Truck' AND d.year=2019
    """)
    val_2024 = int(val_2024 or 0)
    val_2019 = int(val_2019 or 0)
    trk_2024 = int(trk_2024 or 0)
    trk_2019 = int(trk_2019 or 0)
    val_growth = (val_2024 - val_2019) / val_2019 * 100 if val_2019 else None
    trk_growth = (trk_2024 - trk_2019) / trk_2019 * 100 if trk_2019 else None
    ratio = val_growth / trk_growth if trk_growth else None

    # COVID drop: max month value during 2020 vs avg 2019 monthly
    avg_2019_monthly = (val_2019 / 12) if val_2019 else 0
    min_2020 = fetch_scalar(conn, """
        SELECT MIN(monthly) FROM (
            SELECT SUM(f.value_usd) AS monthly
            FROM frontera.fact_transborder f
            JOIN frontera.dim_port p ON f.port_id = p.port_id
            WHERE p.port_canonical_name='Otay Mesa'
              AND f.date_id BETWEEN 20200101 AND 20201201
            GROUP BY f.date_id
        ) sub
    """)
    min_2020 = int(min_2020 or 0)
    covid_drop_pct = (min_2020 - avg_2019_monthly) / avg_2019_monthly * 100 if avg_2019_monthly else None

    # COVID recovery: meses para que un mes vuelva a ≥ avg 2019 monthly tras el min de 2020
    rows = fetch_all(conn, """
        SELECT f.date_id, SUM(f.value_usd) AS v
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name='Otay Mesa'
          AND f.date_id BETWEEN 20200101 AND 20231201
        GROUP BY f.date_id
        ORDER BY f.date_id
    """)
    months_seq = [(int(r["date_id"]), int(r["v"])) for r in rows]
    # encontrar el mes mínimo
    if months_seq and avg_2019_monthly:
        min_idx = min(range(len(months_seq)), key=lambda i: months_seq[i][1])
        recovery_idx = None
        for i in range(min_idx + 1, len(months_seq)):
            if months_seq[i][1] >= avg_2019_monthly:
                recovery_idx = i
                break
        recovery_months = (recovery_idx - min_idx) if recovery_idx is not None else None
    else:
        recovery_months = None

    data = {
        "value_2024_vs_2019_pct": to_float(val_growth, 2),
        "trucks_2024_vs_2019_pct": to_float(trk_growth, 2),
        "ratio_density": to_float(ratio, 2),
        "covid_drop_pct": to_float(covid_drop_pct, 2),
        "covid_recovery_months": recovery_months,
        "method_note": "Otay Mesa, sin filtro data_source (corridor mixto). 2024 y 2019 son años completos para Otay. covid_drop = peor mes 2020 vs avg mensual 2019; recovery_months = meses entre el peor mes y el primero que iguala/supera avg 2019."
    }
    save("section4_recovery/kpis.json", data)


def export_section4_recovery_dual(conn):
    """Serie 2018-2025 mensual con value_usd Otay y trucks Otay."""
    val_rows = fetch_all(conn, """
        SELECT f.date_id, SUM(f.value_usd) AS v
        FROM frontera.fact_transborder f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        WHERE p.port_canonical_name='Otay Mesa'
          AND f.date_id BETWEEN 20180101 AND 20251231
        GROUP BY f.date_id
        ORDER BY f.date_id
    """)
    val_by = {int(r["date_id"]): int(r["v"]) for r in val_rows}

    trk_rows = fetch_all(conn, """
        SELECT f.date_id, SUM(f.crossing_count) AS c
        FROM frontera.fact_border_crossing f
        JOIN frontera.dim_port p ON f.port_id = p.port_id
        JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
        WHERE p.port_canonical_name='Otay Mesa' AND m.mode_canonical_name='Truck'
          AND f.date_id BETWEEN 20180101 AND 20251231
        GROUP BY f.date_id
        ORDER BY f.date_id
    """)
    trk_by = {int(r["date_id"]): int(r["c"]) for r in trk_rows}

    series = []
    for y in range(2018, 2026):
        for m in range(1, 13):
            did = y * 10000 + m * 100 + 1
            iso = f"{y:04d}-{m:02d}-01"
            v = val_by.get(did)
            t = trk_by.get(did)
            if v is None and t is None:
                continue
            series.append({
                "date": iso,
                "value_usd": v,
                "trucks": t,
            })

    save("section4_recovery/recovery_dual.json", {
        "port": "Otay Mesa",
        "monthly": series,
        "note": "Dual axis: value_usd (USD) vs trucks (cruces de camión a US). Una métrica puede ser null si la otra fuente no cubre el mes."
    })


def export_section4_nearshoring_signal(conn):
    """Densidad económica trend: usd/truck mensual con regresión lineal."""
    rows = fetch_all(conn, """
        WITH val AS (
          SELECT f.date_id, SUM(f.value_usd) AS v
          FROM frontera.fact_transborder f
          JOIN frontera.dim_port p ON f.port_id = p.port_id
          WHERE p.port_canonical_name='Otay Mesa' AND f.date_id BETWEEN 20180101 AND 20251231
          GROUP BY f.date_id
        ),
        trk AS (
          SELECT f.date_id, SUM(f.crossing_count) AS c
          FROM frontera.fact_border_crossing f
          JOIN frontera.dim_port p ON f.port_id = p.port_id
          JOIN frontera.dim_mode m ON f.mode_id = m.mode_id
          WHERE p.port_canonical_name='Otay Mesa' AND m.mode_canonical_name='Truck'
            AND f.date_id BETWEEN 20180101 AND 20251231
          GROUP BY f.date_id
        )
        SELECT v.date_id, v.v::float / NULLIF(t.c, 0) AS usd_per_truck
        FROM val v JOIN trk t ON v.date_id = t.date_id
        ORDER BY v.date_id
    """)
    series = []
    for r in rows:
        if r["usd_per_truck"] is None:
            continue
        series.append({
            "date": date_id_to_iso(int(r["date_id"])),
            "usd_per_truck": round(float(r["usd_per_truck"]), 2),
        })

    # Regresión lineal en t (meses desde 2018-01) → y
    if len(series) >= 2:
        xs = list(range(len(series)))
        ys = [s["usd_per_truck"] for s in series]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        sxx = sum((xs[i] - mx) ** 2 for i in range(n))
        slope_per_month = sxy / sxx if sxx else 0
        intercept = my - slope_per_month * mx
        # R²
        ss_res = sum((ys[i] - (slope_per_month * xs[i] + intercept)) ** 2 for i in range(n))
        ss_tot = sum((ys[i] - my) ** 2 for i in range(n))
        r2 = 1 - ss_res / ss_tot if ss_tot else 0
        slope_per_year = slope_per_month * 12
    else:
        slope_per_year = None
        r2 = None

    save("section4_recovery/nearshoring_signal.json", {
        "port": "Otay Mesa",
        "monthly": series,
        "trendline": {
            "slope_usd_per_truck_per_year": round(slope_per_year, 2) if slope_per_year is not None else None,
            "r_squared": round(r2, 3) if r2 is not None else None,
        },
        "method_note": "usd_per_truck mensual = sum(value_usd Otay) / sum(crossings_count Otay Truck). Regresión lineal simple sobre los meses con ambas métricas presentes."
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    OUTPUT_DIR = args.out
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Pre-bake → {OUTPUT_DIR}")
    print(f"Postgres host: {PG['host']}  db: {PG['dbname']}")

    conn = psycopg2.connect(**PG)
    try:
        print("\n[meta]")
        export_meta(conn)

        print("\n[section1 — Panorama]")
        export_section1_kpis(conn)
        export_section1_timeline_value(conn)
        export_section1_timeline_crossings(conn)
        export_section1_puertos_top(conn)

        print("\n[section2 — Otay]")
        export_section2_kpis(conn)
        export_section2_value_monthly(conn)
        export_section2_value_yearly(conn)
        export_section2_mix_sectores(conn)
        export_section2_nearshoring_ratio(conn)
        export_section2_corridor_asimetria(conn)

        print("\n[section3 — Wait]")
        export_section3_kpis(conn)
        export_section3_heatmap(conn)
        export_section3_fast_vs_standard(conn)
        export_section3_tendencia_anual(conn)

        print("\n[section4 — Recovery]")
        export_section4_kpis(conn)
        export_section4_recovery_dual(conn)
        export_section4_nearshoring_signal(conn)

        print(f"\nDone. JSONs en {OUTPUT_DIR}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
