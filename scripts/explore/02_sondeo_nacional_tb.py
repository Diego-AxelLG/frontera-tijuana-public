"""Sondeo del dataset TransBorder nacional (BTS raw). Solo inspección.

NO modifica DB. NO carga facts. Reporta schema, granularidad y volumetría.
"""

from pathlib import Path

import chardet
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NAT = ROOT / "data_samples" / "national_tb" / "extracted"

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 250)


def detect_encoding(path: Path, n: int = 100_000) -> str:
    raw = path.open("rb").read(n)
    return chardet.detect(raw)["encoding"]


def inspect(path: Path, label: str, focus_oct2025: bool = False) -> None:
    enc = detect_encoding(path)
    print(f"\n{'='*90}\n{label}: {path.name}  ({path.stat().st_size:,} bytes, encoding={enc})\n{'='*90}")
    df = pd.read_csv(path, encoding=enc, low_memory=False)
    print(f"shape: {df.shape[0]:,} filas × {df.shape[1]} cols")
    print("\ndtypes + ejemplo (1ra no-null):")
    for c in df.columns:
        ex = df[c].dropna().iloc[0] if df[c].notna().any() else "ALL NULL"
        ex_s = str(ex)[:60]
        print(f"  {c:18s} {str(df[c].dtype):10s}  nulls={df[c].isna().mean()*100:5.2f}%  ex={ex_s!r}")
    print("\nhead(5):")
    print(df.head(5).to_string())

    if focus_oct2025:
        print("\n--- Análisis específico Oct 2025 (preguntas 7-13) ---")

        # Q7: cobertura geográfica
        port_col = next((c for c in df.columns if c.upper() in ("USAPORT", "PORT", "USPORT")), None)
        if port_col:
            ports = sorted(df[port_col].dropna().unique())
            print(f"\nQ7. Universo de ports ({port_col}): {len(ports)} distintos")
            print(f"     primeros 10: {ports[:10]}")
            for needle in ["LARED", "EL PASO", "HIDAL", "BROWN", "OTAY", "SAN YSI", "TECATE", "CALEX"]:
                hits = [p for p in ports if needle in str(p).upper()]
                print(f"     match '{needle}': {hits}")
        # Buscar también por código si hay columna
        code_col = next((c for c in df.columns if c.upper() in ("USAPORT", "PORT_CODE", "PORTCODE", "DOT_CODE", "DISTRICT")), None)
        # USAPORT ya cubre. Veamos si hay códigos numéricos y descripciones
        num_codes = [c for c in df.columns if c.upper() in ("DISTRICT", "DISTRICTCODE", "PORTCODE", "USAPORT")]
        print(f"\n     Columnas tipo code: {num_codes}")

        # Q8: port_code 2501
        for cc in num_codes:
            if df[cc].dtype == "int64" or pd.api.types.is_numeric_dtype(df[cc]):
                hit = df[df[cc] == 2501]
                print(f"\nQ8. {cc}=2501: {len(hit)} filas")
                if len(hit) > 0:
                    other_cols = [c for c in df.columns if c != cc][:6]
                    print(hit[[cc]+other_cols].head(5).to_string())

        # Q9: Granularidad commodity
        commod_cols = [c for c in df.columns if any(k in c.upper() for k in ("COMMODITY","HS","SCHEDULEB","SITC"))]
        print(f"\nQ9. Columnas commodity-related: {commod_cols}")
        for c in commod_cols:
            n_unique = df[c].nunique()
            ex = df[c].dropna().iloc[0] if df[c].notna().any() else None
            print(f"     {c}: n_unique={n_unique:>6} ejemplo={ex!r}  dtype={df[c].dtype}")
            # ¿son códigos numéricos largos?
            if isinstance(ex, (int, float)) or (isinstance(ex, str) and ex.replace('-','').isdigit()):
                lens = df[c].astype(str).str.len()
                print(f"        long. cadena distribución: min={lens.min()} median={int(lens.median())} max={lens.max()}")

        # Q10: geografía origen/destino
        geo_cols = [c for c in df.columns if any(k in c.upper() for k in ("STATE","COUNTRY","ORIGIN","DEST","COFOR","COFRO"))]
        print(f"\nQ10. Columnas geo origen/destino: {geo_cols}")
        for c in geo_cols:
            ex_geo = repr(df[c].dropna().iloc[0]) if df[c].notna().any() else "NULL"
            print(f"     {c}: n_unique={df[c].nunique():>4}  ej={ex_geo}")

        # Q11: modos
        mode_cols = [c for c in df.columns if any(k in c.upper() for k in ("MODE","DOT","TRANSPORT"))]
        print(f"\nQ11. Columnas mode: {mode_cols}")
        for c in mode_cols:
            uniques = sorted(df[c].dropna().unique().tolist())[:30]
            print(f"     {c}: {df[c].nunique()} valores → {uniques}")

        # Q12: dirección
        dir_cols = [c for c in df.columns if any(k in c.upper() for k in ("TRDTYPE","TRADE_TYPE","DIRECTION","IMPORT","EXPORT"))]
        print(f"\nQ12. Columnas direction: {dir_cols}")
        for c in dir_cols:
            print(f"     {c}: {sorted(df[c].dropna().unique().tolist())[:10]}")

        # Q13: filas Otay Mesa Station (port_code=2506) en este archivo
        for cc in num_codes:
            if pd.api.types.is_numeric_dtype(df[cc]):
                otay = df[df[cc] == 2506]
                print(f"\nQ13. {cc}=2506 (Otay Mesa Station) en Oct 2025: {len(otay):,} filas")
                if len(otay) > 0:
                    if commod_cols:
                        print(f"     n_commodities únicas: {otay[commod_cols[0]].nunique()}")
                    if mode_cols:
                        print(f"     modos: {sorted(otay[mode_cols[0]].dropna().unique().tolist())[:20]}")


# =====================================================================
# Inspección
# =====================================================================

# October 2025 — los 3 dot files con foco en Q7-Q13 sobre dot1
print("\n\n############# OCTOBER 2025 (mensual moderno) #############")
inspect(NAT / "oct2025" / "dot1_1025.csv", "dot1 — port × mode × commodity", focus_oct2025=True)
inspect(NAT / "oct2025" / "dot2_1025.csv", "dot2")
inspect(NAT / "oct2025" / "dot3_1025.csv", "dot3")

# March 2024
print("\n\n############# MARCH 2024 (mensual moderno) #############")
inspect(NAT / "mar2024" / "dot1_0324.csv", "dot1")
inspect(NAT / "mar2024" / "dot2_0324.csv", "dot2")

# March 2017 (formato anual desempaquetado)
print("\n\n############# MARCH 2017 (formato 2017 anual) #############")
inspect(NAT / "mar2017" / "March 2017" / "dot1_0317.csv", "dot1 mensual")
inspect(NAT / "mar2017" / "March 2017" / "dot1_ytd_0317.csv", "dot1 YTD acumulado")
