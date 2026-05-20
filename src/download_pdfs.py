"""
Download filing PDFs from eCorp for a given entity.
RATE LIMIT: caller is responsible for 1.5 s delay between entities.

Key findings:
  - Filing history URL: /BusinessSearch/BusinessFilings?businessId={biz_id}
  - Download link: <a href="/BusinessSearch/DownloadFile?filingNo={N}">
  - Download requires a live browser session (page.request.get 403s without it).
  - page.goto() raises "Download is starting" for PDF URLs — use expect_download()
    with page.click() on the link or page.evaluate("window.location.href=...").
  - New entities (< 1yr) typically have exactly one filing: "Business Formation"
    (Articles of Organization + Certificate, combined PDF, 2 pages).
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_URL       = "https://ecorp.sos.ga.gov"
FILINGS_PATH   = "/BusinessSearch/BusinessFilings"
DOWNLOAD_PATH  = "/BusinessSearch/DownloadFile"
RAW_DIR        = Path(__file__).parent.parent / "data" / "raw"
MIN_DELAY      = 1.5

# Filing types we care about (lowercased match against eCorp's filing type string)
TARGET_TYPES = {
    "business formation": "formation",
    "transmittal":        "transmittal",
    "articles":           "articles",
}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def download_entity_pdfs(page, biz_id: str, control_number: str) -> dict[str, Path]:
    """Navigate to the entity's filing page, download target PDFs.

    Returns {doc_type: local_path} for each successfully downloaded PDF.
    Skips files that already exist on disk.
    Caller must have the browser session already warmed up (CF cookies).
    """
    filings = _get_filing_list(page, biz_id)
    logger.info("businessId=%s: %d filing(s) found", biz_id, len(filings))

    results: dict[str, Path] = {}

    for filing in filings:
        doc_type   = filing["doc_type"]
        filing_no  = filing["filing_no"]
        out_path   = RAW_DIR / f"{control_number}_{doc_type}.pdf"

        if out_path.exists() and out_path.stat().st_size > 0:
            logger.info("  %s already exists (%d bytes) — skipping", out_path.name, out_path.stat().st_size)
            results[doc_type] = out_path
            continue

        path = _download_filing(page, biz_id, filing_no, out_path)
        if path:
            results[doc_type] = path
        time.sleep(MIN_DELAY)

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_filing_list(page, biz_id: str) -> list[dict]:
    """Navigate to filing history page and return list of {filing_no, doc_type, filing_date}."""
    url = f"{BASE_URL}{FILINGS_PATH}?businessId={biz_id}"
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(0.8)

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page.content(), "lxml")

    filings: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"filingNo=(\d+)", href)
        if not m:
            continue
        filing_no   = m.group(1)
        filing_type = a.get_text(strip=True)  # e.g. "Business Formation"
        doc_type    = _classify_filing_type(filing_type)

        # Find the date from the surrounding row
        tr = a.find_parent("tr")
        filing_date = ""
        if tr:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            # Table: Filing Number | Filing Date Time | Effective Date | Filing Type
            if len(cells) >= 2:
                filing_date = cells[1]

        # Deduplicate by filing_no
        if not any(f["filing_no"] == filing_no for f in filings):
            filings.append({
                "filing_no":   filing_no,
                "doc_type":    doc_type,
                "filing_type": filing_type,
                "filing_date": filing_date,
            })

    return filings


def _download_filing(page, biz_id: str, filing_no: str, out_path: Path) -> Path | None:
    """Navigate back to filings page, click the target link, capture the download."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    url = f"{BASE_URL}{FILINGS_PATH}?businessId={biz_id}"
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(0.5)

    selector = f"a[href*='filingNo={filing_no}']"
    try:
        with page.expect_download(timeout=30_000) as dl_info:
            page.click(selector)
        dl = dl_info.value
        dl.save_as(str(out_path))
        size = out_path.stat().st_size
        logger.info("  Downloaded %s (%d bytes)", out_path.name, size)
        return out_path
    except Exception as exc:
        logger.warning("  Download failed for filingNo=%s: %s", filing_no, exc)
        return None


def _classify_filing_type(filing_type_str: str) -> str:
    """Map eCorp's verbose filing type to a short doc_type key."""
    s = filing_type_str.lower()
    for fragment, code in TARGET_TYPES.items():
        if fragment in s:
            return code
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "other"


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse
    from rich.console import Console
    from rich.table import Table

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Checkpoint 4: download filing PDFs for a list of entitys"
    )
    parser.add_argument(
        "--biz-ids",
        nargs="+",
        default=["4725721", "4725726", "4725728", "4725729", "4725730"],
        help="businessId values to download (default: 5 from Checkpoint 3)",
    )
    args = parser.parse_args()

    # Map biz_id → control_number using known entities from CP3
    KNOWN = {
        "4725721": "26041493",
        "4725722": "26041497",
        "4725723": "26037708",
        "4725725": "26041375",
        "4725726": "26047327",
        "4725727": "26041378",
        "4725728": "26041380",
        "4725729": "26041383",
        "4725730": "26041386",
        "4725731": "26051974",
    }

    from patchright.sync_api import sync_playwright

    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    console = Console()
    console.print(f"\n[bold cyan]eCorp PDF Downloader — {len(args.biz_ids)} entities[/bold cyan]\n")

    all_results: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--window-position=3000,3000"])
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1280, "height": 900},
            locale="en-US", timezone_id="America/New_York",
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())

        # Warm up (CF cookie)
        page.goto(f"{BASE_URL}/BusinessSearch", wait_until="domcontentloaded", timeout=30_000)
        time.sleep(2)

        for biz_id in args.biz_ids:
            ctrl = KNOWN.get(biz_id, biz_id)
            console.print(f"[dim]businessId={biz_id} ctrl={ctrl}[/dim]")
            results = download_entity_pdfs(page, biz_id, ctrl)
            all_results.append({"biz_id": biz_id, "ctrl": ctrl, "results": results})
            time.sleep(MIN_DELAY)

        browser.close()

    tbl = Table(title="Download Results")
    tbl.add_column("businessId")
    tbl.add_column("Control #")
    tbl.add_column("Files Downloaded")
    tbl.add_column("Sizes")
    for r in all_results:
        files = list(r["results"].values())
        names = [f.name for f in files]
        sizes = [f"{f.stat().st_size:,}" for f in files]
        tbl.add_row(
            r["biz_id"], r["ctrl"],
            "\n".join(names) or "—",
            "\n".join(sizes) or "—",
        )
    console.print(tbl)
