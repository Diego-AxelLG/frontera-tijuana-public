"""Sub-fase 4A.2 — descarga de los ~108 ZIPs de TransBorder vía Wayback.

Uso:
  python etl/02_download_national.py discover   # solo discovery
  python etl/02_download_national.py            # discovery + download + verify
"""

import json
import re
import sys
import time
import zipfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data_raw" / "transborder_national"
LOGS = ROOT / "etl" / "logs"
REPORTS = ROOT / "reports"
RAW.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(exist_ok=True)

LOG_FILE = LOGS / "download_national.log"
NOT_ARCHIVED = LOGS / "not_archived.txt"
CORRUPT = LOGS / "corrupt.txt"
SUMMARY_JSON = REPORTS / "phase4a2_summary.json"

WAYBACK_LISTING = "https://web.archive.org/web/2025im_/https://www.bts.gov/topics/transborder-raw-data"

# Captura tanto URLs absolutas (https://www.bts.gov/sites/...) como relativas (/sites/...)
URL_PATTERN = re.compile(
    r'(?:https?://www\.bts\.gov)?/sites/bts\.dot\.gov/files/transborder-raw/(\d{4})/[^/\s"\'<>]+?\.zip',
    re.IGNORECASE,
)
ABS_PATTERN = re.compile(
    r'https?://www\.bts\.gov/sites/bts\.dot\.gov/files/transborder-raw/(\d{4})/[^/\s"\'<>]+?\.zip',
    re.IGNORECASE,
)

THROTTLE_SEC = 1.5
CDX_TIMEOUT = 30
DL_TIMEOUT = 600  # archivos anuales son grandes, dar margen


def log(msg: str, also_print: bool = True) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if also_print:
        print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch_with_retries(url: str, *, stream: bool = False, max_retries: int = 4, timeout: int = 60):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, stream=stream, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            wait = 2 ** attempt
            log(f"   fetch attempt {attempt+1}/{max_retries} fallo ({type(e).__name__}: {str(e)[:120]}). esperando {wait}s")
            time.sleep(wait)
    return None


# =====================================================================
# Discovery
# =====================================================================

def discover_urls() -> list[tuple[str, str]]:
    """Devuelve lista ordenada y deduplicada de (year, original_url)."""
    log(f"discover: GET {WAYBACK_LISTING}")
    r = fetch_with_retries(WAYBACK_LISTING, timeout=90)
    if r is None:
        log("discover: ERROR — no se pudo obtener el listado de Wayback.")
        return []

    html = r.text

    # Estrategia 1: regex directo sobre el HTML crudo (captura URLs en hrefs reescritos por Wayback y en hrefs relativos).
    candidates = set()
    for m in URL_PATTERN.finditer(html):
        match = m.group(0)
        if match.startswith("/"):
            full = "https://www.bts.gov" + match
        else:
            full = match
        candidates.add(full)

    # Estrategia 2 (refuerzo): BeautifulSoup sobre los <a href> visibles, por si hay algo en atributos no captado por el regex anterior.
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Si está reescrito por Wayback, extrae el original
        m_abs = ABS_PATTERN.search(href)
        if m_abs:
            candidates.add(m_abs.group(0))
            continue
        # Sin reescribir y con path relativo
        m_rel = URL_PATTERN.search(href)
        if m_rel and m_rel.group(0).startswith("/"):
            candidates.add("https://www.bts.gov" + m_rel.group(0))

    if not candidates:
        log("discover: 0 URLs encontradas. Detenerse.")
        return []

    # Construir tuplas (year, url) ordenadas por year ASC y luego nombre
    result = []
    for url in sorted(candidates):
        m = re.search(r"/transborder-raw/(\d{4})/", url)
        if m:
            result.append((m.group(1), url))

    log(f"discover: {len(result)} URLs encontradas.")
    log(f"  primera: {result[0][1]}")
    log(f"  última : {result[-1][1]}")
    log(f"  años cubiertos: {sorted({y for y, _ in result})}")

    # Distribución por año
    from collections import Counter
    by_year = Counter(y for y, _ in result)
    for y in sorted(by_year):
        log(f"    {y}: {by_year[y]} archivos", also_print=False)
    return result


# =====================================================================
# Wayback CDX → snapshot más reciente
# =====================================================================

def get_latest_snapshot(url: str) -> str | None:
    cdx = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={url}&output=json&limit=-1&filter=statuscode:200"
    )
    r = fetch_with_retries(cdx, timeout=CDX_TIMEOUT, max_retries=3)
    if r is None:
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    if len(data) < 2:
        return None
    # data[0] es header, data[-1] es el más reciente cuando limit=-1
    return data[-1][1]


# =====================================================================
# Descarga + verify
# =====================================================================

def download_with_resume(url: str, dest: Path, max_retries: int = 5) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        return True
    for attempt in range(max_retries):
        try:
            r = requests.get(url, stream=True, timeout=DL_TIMEOUT)
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            tmp.rename(dest)
            return True
        except (requests.RequestException, IOError) as e:
            log(f"   download attempt {attempt+1}/{max_retries} fallo ({type(e).__name__}: {str(e)[:120]})")
            for p in (dest.with_suffix(dest.suffix + ".part"), dest):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            wait = 2 ** attempt
            time.sleep(wait)
    return False


def verify_zip(path: Path) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except Exception:
        return False


def dest_for(year: str, original_url: str) -> Path:
    basename = original_url.rstrip("/").split("/")[-1]
    return RAW / f"{year}__{basename}"


# =====================================================================
# Main download loop
# =====================================================================

def run_downloads(targets: list[tuple[str, str]]) -> dict:
    not_archived: list[str] = []
    corrupt: list[str] = []
    ok: list[str] = []
    failed: list[str] = []
    skipped_existing: list[str] = []

    pbar = tqdm(targets, desc="download", unit="zip")
    for year, url in pbar:
        dest = dest_for(year, url)
        pbar.set_postfix_str(dest.name[:50])

        # Skip si ya existe íntegro (idempotencia)
        if dest.exists() and dest.stat().st_size > 1000 and verify_zip(dest):
            skipped_existing.append(dest.name)
            continue

        log(f"\n>>> {url}")

        ts = get_latest_snapshot(url)
        if ts is None:
            log(f"   not_archived")
            not_archived.append(url)
            continue

        wb_url = f"https://web.archive.org/web/{ts}im_/{url}"
        log(f"   snapshot ts={ts}")
        log(f"   GET {wb_url}")

        if not download_with_resume(wb_url, dest):
            failed.append(url)
            continue

        if not verify_zip(dest):
            log(f"   integridad falló, reintentando una vez…")
            dest.unlink()
            if download_with_resume(wb_url, dest) and verify_zip(dest):
                size_mb = dest.stat().st_size / 1e6
                log(f"   OK retry  ({size_mb:.2f} MB)")
                ok.append(dest.name)
            else:
                log(f"   CORRUPTO tras reintento")
                corrupt.append(url)
                if dest.exists():
                    dest.unlink()
        else:
            size_mb = dest.stat().st_size / 1e6
            log(f"   OK ({size_mb:.2f} MB)")
            ok.append(dest.name)

        # Throttle (solo si hubo download real, no si fue skip)
        time.sleep(THROTTLE_SEC)

        # Resumen periódico
        if (len(ok) + len(failed) + len(corrupt)) % 10 == 0 and (len(ok) + len(failed) + len(corrupt)) > 0:
            cum_mb = sum(p.stat().st_size for p in RAW.glob("*.zip")) / 1e6
            log(f"   progreso: ok={len(ok)} failed={len(failed)} corrupt={len(corrupt)} skipped={len(skipped_existing)} | dir={cum_mb:.0f} MB")

    NOT_ARCHIVED.write_text("\n".join(not_archived) + "\n" if not_archived else "")
    CORRUPT.write_text("\n".join(corrupt) + "\n" if corrupt else "")
    return {
        "ok": len(ok),
        "failed": len(failed),
        "corrupt": len(corrupt),
        "not_archived": len(not_archived),
        "skipped_existing": len(skipped_existing),
        "ok_files": ok,
        "failed_urls": failed,
        "corrupt_urls": corrupt,
        "not_archived_urls": not_archived,
    }


# =====================================================================
# Validaciones obligatorias
# =====================================================================

def validate_post_download() -> dict:
    files = list(RAW.glob("*.zip"))
    n_files = len(files)
    total_mb = sum(f.stat().st_size for f in files) / 1e6

    # Sanity por año
    from collections import defaultdict
    by_year = defaultdict(lambda: {"count": 0, "mb": 0.0})
    for f in files:
        m = re.match(r"^(\d{4})__", f.name)
        if not m:
            continue
        y = m.group(1)
        by_year[y]["count"] += 1
        by_year[y]["mb"] += f.stat().st_size / 1e6

    by_year_sorted = {y: by_year[y] for y in sorted(by_year)}

    # Verificar todos otra vez (rápido en serie)
    bad = []
    for f in files:
        if not verify_zip(f):
            bad.append(f.name)

    log(f"\nValidación: n_files={n_files} total_mb={total_mb:.1f} bad={len(bad)}")
    return {
        "n_files": n_files,
        "total_mb": round(total_mb, 1),
        "by_year": {y: {"count": v["count"], "mb": round(v["mb"], 1)} for y, v in by_year_sorted.items()},
        "bad_files": bad,
    }


# =====================================================================
# Entry
# =====================================================================

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in {"discover", "all"}:
        print(f"modo desconocido: {mode}. Use 'discover' o 'all'.")
        return 2

    log(f"\n{'='*70}\nINICIO modo={mode}\n{'='*70}")

    targets = discover_urls()
    if len(targets) < 100:
        log(f"discover devolvió {len(targets)} URLs (< 100). Stop-and-report.")
        SUMMARY_JSON.write_text(json.dumps({"discovered": len(targets), "aborted": True}, indent=2))
        return 1

    if mode == "discover":
        log("modo discover — no se baja nada. salgo.")
        SUMMARY_JSON.write_text(json.dumps({
            "discovered": len(targets),
            "first_5": [u for _, u in targets[:5]],
            "last_5": [u for _, u in targets[-5:]],
        }, indent=2))
        return 0

    started = time.time()
    dl = run_downloads(targets)
    val = validate_post_download()
    elapsed = time.time() - started

    summary = {
        "discovered": len(targets),
        "first_5": [u for _, u in targets[:5]],
        "last_5": [u for _, u in targets[-5:]],
        "elapsed_sec": round(elapsed, 1),
        "elapsed_min": round(elapsed / 60, 2),
        "download": dl,
        "validation": val,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))
    log(f"\nFIN. summary -> {SUMMARY_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
