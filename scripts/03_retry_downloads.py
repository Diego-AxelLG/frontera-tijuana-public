"""Sub-fase 4A.2-RETRY — recuperación agresiva de archivos faltantes."""

import json
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data_raw" / "transborder_national"
LOGS = ROOT / "etl" / "logs"
RETRY_LOG = LOGS / "download_retry.log"
RETRY_JSONL = LOGS / "retry_results.jsonl"
SUMMARY_JSON = ROOT / "reports" / "phase4a2_summary.json"
RETRY_REPORT = ROOT / "reports" / "phase4a2_retry_summary.json"

# Reset de logs (esta corrida vive en su propio archivo)
RETRY_LOG.write_text("")
RETRY_JSONL.write_text("")

# Config agresiva
THROTTLE_SEC = 5.0
CDX_BACKOFF = [1, 2, 4, 8, 16, 32, 60, 60]
DL_BACKOFF  = [1, 2, 4, 8, 16, 32, 60, 60]
DL_TIMEOUT  = (30, 1200)   # (connect, read)
CDX_TIMEOUT = (30, 60)

SAVE_TARGETS_2025 = [
    "https://www.bts.gov/sites/bts.dot.gov/files/transborder-raw/2025/January2025.zip",
    "https://www.bts.gov/sites/bts.dot.gov/files/transborder-raw/2025/Feb2025.zip",
    "https://www.bts.gov/sites/bts.dot.gov/files/transborder-raw/2025/March2025.zip",
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(RETRY_LOG, "a") as f:
        f.write(line + "\n")


def jsonl_append(rec: dict) -> None:
    with open(RETRY_JSONL, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def fetch_with_retries(url, *, stream=False, timeout=None, backoff=None):
    backoff = backoff or [1, 2, 4]
    for i, wait in enumerate(backoff):
        try:
            r = requests.get(url, stream=stream, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log(f"   fetch {i+1}/{len(backoff)} fallo ({type(e).__name__}: {str(e)[:100]}); espera {wait}s")
            time.sleep(wait)
    return None


def cdx_latest(url: str) -> str | None:
    cdx = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={url}&output=json&limit=-1&filter=statuscode:200"
    )
    r = fetch_with_retries(cdx, timeout=CDX_TIMEOUT, backoff=CDX_BACKOFF)
    if r is None:
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    return data[-1][1] if len(data) > 1 else None


def download_with_resume(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        return True
    for i, wait in enumerate(DL_BACKOFF):
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
            log(f"   dl {i+1}/{len(DL_BACKOFF)} fallo ({type(e).__name__}: {str(e)[:100]})")
            for p in (dest.with_suffix(dest.suffix + ".part"), dest):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            time.sleep(wait)
    return False


def verify_zip(p: Path) -> bool:
    if not zipfile.is_zipfile(p):
        return False
    try:
        with zipfile.ZipFile(p) as zf:
            return zf.testzip() is None
    except Exception:
        return False


def dest_for(year: str, url: str) -> Path:
    basename = url.rstrip("/").split("/")[-1]
    return RAW / f"{year}__{basename}"


def trigger_save(url: str) -> str:
    """Fire-and-forget POST to Wayback Save Page Now."""
    try:
        r = requests.get(
            f"https://web.archive.org/save/{url}",
            timeout=10,
            allow_redirects=False,
            headers={"User-Agent": "frontera-tijuana-bot/1.0"},
        )
        return f"{r.status_code}"
    except requests.Timeout:
        return "timeout(saved-async-probably)"
    except Exception as e:
        return f"err:{type(e).__name__}"


def build_retry_list() -> list[tuple[str, str, str]]:
    summary = json.loads(SUMMARY_JSON.read_text())
    dl = summary["download"]
    seen = set()
    out = []
    for cat, key in [("not_archived", "not_archived_urls"),
                     ("corrupt", "corrupt_urls"),
                     ("failed", "failed_urls")]:
        for url in dl.get(key, []):
            if url in seen:
                continue
            seen.add(url)
            m = re.search(r"/transborder-raw/(\d{4})/", url)
            if not m:
                continue
            year = int(m.group(1))
            if year < 2006:
                log(f"  skip pre-2006: {url}")
                continue
            out.append((str(year).zfill(4), url, cat))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def coverage_table(files: list[Path]) -> list[dict]:
    by_year = Counter()
    for f in files:
        m = re.match(r"^(\d{4})__", f.name)
        if m:
            by_year[m.group(1)] += 1
    expected = {str(y): 1 for y in range(2006, 2018)}
    expected.update({str(y): 12 for y in [2018, 2019, 2020, 2022, 2023, 2024]})
    expected["2021"] = 8
    expected["2025"] = 3
    rows = []
    for y in sorted(expected):
        ok = by_year.get(y, 0)
        exp = expected[y]
        pct = round(100 * ok / exp, 1) if exp else 0
        rows.append({"year": y, "expected": exp, "ok": ok, "pct": pct})
    return rows


def main() -> int:
    log(f"=== START retry @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    # 1) Trigger /save/ para los 3 URLs 2025 (sin esperar)
    log("\n--- Triggering /save/ para 3 URLs de 2025 (fire-and-forget) ---")
    save_results = {}
    for u in SAVE_TARGETS_2025:
        rc = trigger_save(u)
        save_results[u] = rc
        log(f"  save {u.split('/')[-1]:25s} -> {rc}")
        time.sleep(1)

    # 2) Build retry list
    targets = build_retry_list()
    log(f"\n--- Retry list: {len(targets)} URLs ---")
    for y, u, cat in targets:
        log(f"  [{cat:13s}] {y} {u.split('/')[-1]}")

    results = {
        "recovered_from": {"not_archived": 0, "corrupt": 0, "failed": 0},
        "still_missing": [],
        "save_triggered": save_results,
        "save_followup": {},
        "retry_list_size": len(targets),
    }

    # 3) Main retry loop
    for y, url, original_cat in targets:
        dest = dest_for(y, url)
        started = time.time()
        log(f"\n>>> [{original_cat}] {url}")
        rec = {
            "url": url, "year": y, "original_category": original_cat,
            "dest": dest.name, "started_at": time.strftime("%H:%M:%S"),
            "attempts": {"cdx": 0, "download": 0},
        }

        if dest.exists() and dest.stat().st_size > 1000 and verify_zip(dest):
            log(f"   ya existe e íntegro, skip")
            rec["status"] = "already_exists"
            rec["mb_downloaded"] = round(dest.stat().st_size / 1e6, 2)
            rec["total_seconds"] = 0
            jsonl_append(rec)
            continue

        ts = cdx_latest(url)
        if ts is None:
            log(f"   CDX vacío → not_archived (genuino o timeout persistente)")
            rec["status"] = "not_archived"
            rec["mb_downloaded"] = 0
            rec["total_seconds"] = round(time.time() - started, 1)
            jsonl_append(rec)
            results["still_missing"].append({"url": url, "reason": "not_archived"})
            time.sleep(THROTTLE_SEC)
            continue

        wb_url = f"https://web.archive.org/web/{ts}im_/{url}"
        log(f"   snapshot ts={ts}")
        log(f"   GET {wb_url}")

        if not download_with_resume(wb_url, dest):
            log(f"   download FAIL tras todos los retries")
            rec["status"] = "download_failed"
            rec["mb_downloaded"] = 0
            rec["total_seconds"] = round(time.time() - started, 1)
            jsonl_append(rec)
            results["still_missing"].append({"url": url, "reason": "download_failed"})
            time.sleep(THROTTLE_SEC)
            continue

        if not verify_zip(dest):
            log(f"   integridad ZIP falla, reintento una vez")
            dest.unlink()
            if download_with_resume(wb_url, dest) and verify_zip(dest):
                size_mb = round(dest.stat().st_size / 1e6, 2)
                log(f"   OK after retry  ({size_mb} MB)")
                rec["status"] = "ok_after_retry"
                rec["mb_downloaded"] = size_mb
                results["recovered_from"][original_cat] += 1
            else:
                log(f"   CORRUPTO tras reintento")
                rec["status"] = "corrupt"
                rec["mb_downloaded"] = 0
                results["still_missing"].append({"url": url, "reason": "corrupt"})
                if dest.exists():
                    dest.unlink()
        else:
            size_mb = round(dest.stat().st_size / 1e6, 2)
            log(f"   OK ({size_mb} MB)")
            rec["status"] = "ok"
            rec["mb_downloaded"] = size_mb
            results["recovered_from"][original_cat] += 1

        rec["total_seconds"] = round(time.time() - started, 1)
        jsonl_append(rec)
        time.sleep(THROTTLE_SEC)

    # 4) Re-check /save/ status para los 3 URLs 2025
    log("\n--- Re-check /save/ status para 3 URLs 2025 ---")
    for u in SAVE_TARGETS_2025:
        ts = cdx_latest(u)
        results["save_followup"][u] = ts or "still_not_archived"
        log(f"  {u.split('/')[-1]:25s} -> {results['save_followup'][u]}")
        time.sleep(2)

    # 5) Cobertura final
    files = list(RAW.glob("*.zip"))
    total_mb = round(sum(f.stat().st_size for f in files) / 1e6, 1)
    results["total_files"] = len(files)
    results["total_mb"] = total_mb
    results["coverage_2006plus"] = coverage_table(files)

    RETRY_REPORT.write_text(json.dumps(results, indent=2, default=str))

    log(f"\n{'='*60}\nRESUMEN")
    log(f"  archivos en dir ahora: {len(files)} ({total_mb} MB)")
    log(f"  recovered_from_not_archived: {results['recovered_from']['not_archived']}")
    log(f"  recovered_from_corrupt:      {results['recovered_from']['corrupt']}")
    log(f"  recovered_from_failed:       {results['recovered_from']['failed']}")
    log(f"  still_missing:               {len(results['still_missing'])}")
    log(f"  summary JSON: {RETRY_REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
