"""Sub-fase 4A.3.1 — extracción de los 98 ZIPs.

Estrategia:
- Filtrar a year >= 2006 (descartar 1993-2005)
- Era mensual (2018+): ZIP contiene dot1/dot2/dot3 directos
- Era anual (2006-2017): ZIP contiene 12 ZIPs internos por mes
- Extraer SOLO dot3 (port × commodity × mode), descartar _ytd_
- Resultado: extracted/{YYYY-MM}/dot3_*.csv
"""

import re
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data_raw" / "transborder_national"
OUT = RAW / "extracted"
OUT.mkdir(exist_ok=True)

# Regex: dot3_MMYY.csv → date_id mensual
DOT3_RE = re.compile(r"^dot3_(\d{2})(\d{2})\.csv$", re.IGNORECASE)
ZIP_INNER_RE = re.compile(r"\.zip$", re.IGNORECASE)
YTD_RE = re.compile(r"_ytd_", re.IGNORECASE)


def yymm_to_yyyymm(mm: str, yy: str) -> str:
    """Convierte MM/YY desde nombre dot3 a YYYY-MM. Asume 06-99 = 19xx-20xx."""
    yi = int(yy)
    yyyy = 2000 + yi if yi <= 30 else 1900 + yi  # 06-30 → 2006-2030, 31-99 → 1931-1999
    return f"{yyyy:04d}-{int(mm):02d}"


def is_valid_dot3_for_scope(name: str, hint_year: int | None = None) -> tuple[bool, str | None]:
    """Devuelve (keep, yyyymm). Keep si es dot3, no es ytd, y year >= 2006."""
    if YTD_RE.search(name):
        return (False, None)
    base = name.split("/")[-1]
    m = DOT3_RE.match(base)
    if not m:
        return (False, None)
    mm, yy = m.group(1), m.group(2)
    # Validar que mm sea mes válido (1-12). Esto descarta yearly summaries tipo dot3_2009.csv
    # donde regex captura (20, 09).
    mm_int = int(mm)
    if not (1 <= mm_int <= 12):
        return (False, None)
    yyyymm = yymm_to_yyyymm(mm, yy)
    yyyy = int(yyyymm[:4])
    if yyyy < 2006:
        return (False, None)
    return (True, yyyymm)


def extract_zip_recursive(zip_path: Path, hint_year: int, depth: int = 0) -> int:
    """Extrae dot3 CSVs (escaneando inner ZIPs si es anual). Devuelve cuántos extrajo.

    Tolera EOFError y BadZipFile dentro de los inner ZIPs (algunos vienen truncados de Wayback).
    """
    count = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                # Nivel directo: ¿es dot3?
                keep, yyyymm = is_valid_dot3_for_scope(name)
                if keep:
                    target_dir = OUT / yyyymm
                    target_dir.mkdir(exist_ok=True)
                    target_file = target_dir / Path(name).name
                    if target_file.exists():
                        continue
                    try:
                        with zf.open(info) as src, open(target_file, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        count += 1
                    except (EOFError, zipfile.BadZipFile) as e:
                        print(f"    EOF/BadZip extrayendo {name} de {zip_path.name}: {type(e).__name__}")
                        if target_file.exists():
                            target_file.unlink()
                    continue
                # Inner ZIP (era anual): extraer en /tmp y recursar
                if ZIP_INNER_RE.search(name):
                    tmp_inner = RAW / f"_tmp_inner_{depth}.zip"
                    try:
                        with zf.open(info) as src, open(tmp_inner, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        count += extract_zip_recursive(tmp_inner, hint_year, depth + 1)
                    except (EOFError, zipfile.BadZipFile) as e:
                        print(f"    EOF/BadZip en inner ZIP {name} de {zip_path.name}: {type(e).__name__}")
                    finally:
                        tmp_inner.unlink(missing_ok=True)
    except (zipfile.BadZipFile, EOFError) as e:
        print(f"  SKIP top-level {zip_path.name}: {type(e).__name__}")
    return count


def main() -> int:
    zips = sorted(RAW.glob("*.zip"))
    print(f"Encontrados {len(zips)} ZIPs en {RAW}")

    pre_2006 = [z for z in zips if int(z.name[:4]) < 2006]
    in_scope = [z for z in zips if int(z.name[:4]) >= 2006]
    print(f"  pre-2006 a IGNORAR: {len(pre_2006)}")
    print(f"  in-scope (>=2006): {len(in_scope)}")

    total_extracted = 0
    for i, z in enumerate(in_scope, 1):
        year_hint = int(z.name[:4])
        before = total_extracted
        total_extracted += extract_zip_recursive(z, year_hint)
        if i % 10 == 0 or i == len(in_scope):
            print(f"  [{i:3d}/{len(in_scope)}] {z.name[:60]:60s} +{total_extracted-before} dot3, total={total_extracted}")

    # Resumen
    dot3_files = sorted(OUT.glob("*/dot3_*.csv"))
    months_with_data = sorted({p.parent.name for p in dot3_files})
    print(f"\n=== RESUMEN ===")
    print(f"  Total dot3 extraídos: {len(dot3_files)}")
    print(f"  Meses únicos representados: {len(months_with_data)}")
    print(f"  Primeros 5: {months_with_data[:5]}")
    print(f"  Últimos 5:  {months_with_data[-5:]}")

    # Detectar gaps en serie
    yymm_set = set(months_with_data)
    expected = []
    for y in range(2006, 2025):
        for m in range(1, 13):
            expected.append(f"{y:04d}-{m:02d}")
    expected.append("2025-01")
    expected.append("2025-02")
    expected.append("2025-03")
    gaps = sorted(set(expected) - yymm_set)
    extra = sorted(yymm_set - set(expected))
    print(f"\n  Esperados (2006-01 → 2025-03): {len(expected)} meses")
    print(f"  Gaps en extracted: {len(gaps)} → {gaps[:20]}{'...' if len(gaps)>20 else ''}")
    if extra:
        print(f"  Extra (no esperados): {extra}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
