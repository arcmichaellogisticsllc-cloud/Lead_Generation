"""
Discover new GA entity control numbers by scanning eCorp detail pages sequentially.
RATE LIMIT: 1.5 s minimum between page loads. Single-threaded.

Key findings from eCorp reverse-engineering:
  - ControlNo "StartsWith" search: server disables criteria radios and returns the
    form page for prefix searches — only ExactMatch is supported for ControlNo.
  - BusinessName search results are unsorted (mixed years) — watermark early-stop
    is not possible with search-based discovery.
  - Direct businessId scan: navigate /BusinessSearch/BusinessInformation?businessId=N
    for each N. Valid entities return a page with an 8-digit control number in the HTML.
    Invalid/non-existent IDs return the search form template (no control number).
  - businessIds are dense (sequential, no gaps in active range).
  - Control numbers are year-prefixed: "26XXXXXX" = 2026 entity.

Strategy:
  1. Load watermark (highest businessId seen) from state.json.
  2. Scan businessIds sequentially from watermark+1.
  3. For each ID, navigate to the detail page and extract ctrl_num + basic info.
  4. Yield entities whose ctrl_num starts with the current year prefix ("26").
  5. Stop after MAX_CONSECUTIVE_MISSES consecutive IDs without a valid ctrl_num
     (signals we've passed the frontier of registered businesses).
  6. Persist the highest businessId seen as the new watermark.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL   = "https://ecorp.sos.ga.gov"
DETAIL_URL = f"{BASE_URL}/BusinessSearch/BusinessInformation"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MIN_DELAY             = 1.5
MAX_SCAN_PER_RUN      = int(__import__("os").environ.get("DISCOVER_SCAN", "500"))
MAX_CONSECUTIVE_MISSES = 20   # stop if this many consecutive IDs lack a valid ctrl_num
MAX_RESULTS_PER_RUN   = None  # None = unlimited; set to int for early exit (CLI demo)

# Estimated businessId of the first 2026 entity.  Used only on first-ever run (no
# watermark).  Set conservatively low — the bootstrap script rescans the full range.
INITIAL_2026_BIZ_ID = 4_700_000

STATE_FILE      = Path(__file__).parent.parent / "state.json"
SCREENSHOTS_DIR = Path(__file__).parent.parent / "data" / "debug"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _year_prefix() -> str:
    """Two-digit year prefix used in GA control numbers (e.g. '26' for 2026)."""
    return str(date.today().year)[2:]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def discover_new_entities(start_date: date, end_date: date) -> list[dict]:
    """Return new GA 2026 entities filed since the last run (watermark-based).

    Scans businessIds sequentially from the watermark forward, extracting basic
    entity data from each detail page.  Only entities whose control number starts
    with the current year prefix are returned.

    Exact date filtering (start_date / end_date) is applied where formation_date
    is available from the detail page.

    Returns list of {control_number, ecorp_control_number, entity_name,
                     entity_type, status, formation_date}.
    control_number is the eCorp businessId used in detail page URLs.
    """
    from src.db import get_connection, DB_PATH

    conn = get_connection(DB_PATH)
    existing = {
        row[0]
        for row in conn.execute("SELECT control_number FROM leads").fetchall()
    }
    conn.close()
    logger.info("DB has %d existing control numbers", len(existing))

    state     = _load_state()
    watermark = state.get("watermark")
    if watermark is None:
        watermark = str(INITIAL_2026_BIZ_ID - 1)
        logger.info("No watermark found — starting from businessId %s", watermark)

    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--window-position=3000,3000"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())

        entities, new_watermark = _scan_business_ids(
            page, watermark, existing, start_date, end_date
        )
        browser.close()

    state["last_run"]   = str(date.today())
    state["last_start"] = str(start_date)
    state["last_end"]   = str(end_date)
    state["last_count"] = len(entities)
    if new_watermark:
        state["watermark"] = new_watermark
    _save_state(state)

    logger.info("Discovery complete — %d new entities", len(entities))
    return entities


def _max_results() -> int | None:
    """Return the per-run entity cap (overridable via env var DISCOVER_MAX)."""
    import os
    v = os.environ.get("DISCOVER_MAX")
    return int(v) if v else MAX_RESULTS_PER_RUN


# ---------------------------------------------------------------------------
# BusinessId scanner
# ---------------------------------------------------------------------------

def _scan_business_ids(
    page,
    watermark: str,
    existing: set[str],
    start_date: date,
    end_date: date,
) -> tuple[list[dict], str | None]:
    """Scan businessIds from watermark+1, return valid new 2026 entities."""
    year_prefix = _year_prefix()
    start_id    = int(watermark) + 1
    end_id      = start_id + MAX_SCAN_PER_RUN

    entities:      list[dict] = []
    new_watermark: str | None = None
    consecutive_misses        = 0

    logger.info(
        "Scanning businessIds %d – %d (year_prefix=%s)",
        start_id, end_id, year_prefix,
    )

    for biz_id in range(start_id, end_id + 1):
        entity = _load_entity_page(page, biz_id)

        if entity is None:
            # Invalid/non-existent businessId
            consecutive_misses += 1
            if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                logger.info(
                    "Reached frontier after %d consecutive misses at businessId %d",
                    MAX_CONSECUTIVE_MISSES, biz_id,
                )
                break
            time.sleep(MIN_DELAY)
            continue

        consecutive_misses = 0
        new_watermark = str(biz_id)

        ctrl = entity.get("ecorp_control_number", "") or ""
        if not ctrl.startswith(year_prefix):
            logger.debug("businessId %d: ctrl=%s — not a %s entity, skipping", biz_id, ctrl, year_prefix)
            time.sleep(MIN_DELAY)
            continue

        biz_id_str = str(biz_id)
        if biz_id_str in existing:
            logger.debug("businessId %d already in DB", biz_id)
            time.sleep(MIN_DELAY)
            continue

        # Date filter
        fd = entity.get("formation_date")
        if fd:
            try:
                fd_date = date.fromisoformat(fd)
                if not (start_date <= fd_date <= end_date):
                    logger.debug("businessId %d: formation_date %s outside range", biz_id, fd)
                    time.sleep(MIN_DELAY)
                    continue
            except ValueError:
                pass  # keep entity if date can't be parsed

        entities.append(entity)
        logger.info(
            "New entity: businessId=%s ctrl=%s formed=%s name=%s",
            biz_id, ctrl, entity.get("formation_date") or "?",
            entity.get("entity_name", "")[:40],
        )

        _screenshot(page, f"entity_{biz_id}")

        cap = _max_results()
        if cap is not None and len(entities) >= cap:
            logger.info("Reached DISCOVER_MAX=%d — stopping early", cap)
            break

        time.sleep(MIN_DELAY)

    return entities, new_watermark


# ---------------------------------------------------------------------------
# Detail page loader
# ---------------------------------------------------------------------------

def _load_entity_page(page, biz_id: int) -> dict | None:
    """Navigate to a detail page and extract basic entity info.

    Returns None if the page does not contain a valid entity (no ctrl_num found).
    """
    url = f"{DETAIL_URL}?businessId={biz_id}&fromSearch=True"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        logger.debug("businessId %d load error: %s", biz_id, exc)
        return None

    time.sleep(0.5)
    html = page.content()

    # Check for Cloudflare challenge page — back off and retry once
    if "security verification" in html.lower():
        logger.debug("businessId %d: CF challenge, waiting 3s...", biz_id)
        time.sleep(3)
        html = page.content()

    soup = BeautifulSoup(html, "lxml")

    # Extract control number — its presence distinguishes a real entity from a
    # "not found" response (which returns the form template without any data).
    ctrl_num = _extract_ctrl_num(soup)
    if ctrl_num is None:
        logger.debug("businessId %d: no control number found (invalid/not found)", biz_id)
        return None

    entity_name  = _extract_entity_name(soup)
    entity_type  = _extract_field(soup, ("business type", "entity type", "type"))
    status       = _extract_field(soup, ("business status", "status"))
    formation_dt = _extract_field(soup, (
        "date of formation / registration date",
        "date of organization",
        "formation date",
        "filing date",
        "date filed",
    ))

    return {
        "control_number":      str(biz_id),
        "ecorp_control_number": ctrl_num,
        "entity_name":         entity_name,
        "entity_type":         entity_type,
        "status":              status,
        "formation_date":      _parse_date(formation_dt),
    }


# ---------------------------------------------------------------------------
# Page parsing helpers
# ---------------------------------------------------------------------------

def _extract_ctrl_num(soup) -> str | None:
    """Find an 8-digit GA control number in the page."""
    text = soup.get_text(" ")
    m = re.search(r'\b(\d{8})\b', text)
    return m.group(1) if m else None


def _extract_entity_name(soup) -> str | None:
    for label in ("business name", "entity name", "name"):
        v = _extract_field(soup, (label,))
        if v:
            return v
    # Fallback: heading element
    for tag in ("h1", "h2"):
        el = soup.find(tag)
        if el:
            txt = el.get_text(strip=True)
            if txt and len(txt) > 2:
                return txt
    return None


def _extract_field(soup, labels: tuple) -> str | None:
    """Extract a field value from dt/dd pairs or table rows (2-col or 4-col layout).

    eCorp detail pages use a 4-column table: label|value|label|value per row.
    """
    labels_set = {l.lower().rstrip(":") for l in labels}

    # dt/dd pattern
    for dt in soup.find_all("dt"):
        lbl = dt.get_text(strip=True).lower().rstrip(":")
        if lbl in labels_set:
            dd = dt.find_next_sibling("dd")
            if dd:
                return dd.get_text(separator=" ", strip=True)

    # Table pattern — check columns 0+1 AND 2+3 (4-column eCorp layout)
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            lbl = cells[0].get_text(strip=True).lower().rstrip(":")
            if lbl in labels_set:
                v = cells[1].get_text(separator=" ", strip=True)
                if v:
                    return v
        if len(cells) >= 4:
            lbl = cells[2].get_text(strip=True).lower().rstrip(":")
            if lbl in labels_set:
                v = cells[3].get_text(separator=" ", strip=True)
                if v:
                    return v
    return None


def _parse_date(s: str | None) -> str | None:
    if not s:
        return None
    from datetime import datetime
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s or None


def _screenshot(page, name: str) -> None:
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOTS_DIR / f"{name}.png"), full_page=True)
    except Exception as exc:
        logger.debug("Screenshot: %s", exc)


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from rich.console import Console
    from rich.table import Table

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Checkpoint 3: discover GA entities for current year (shows up to 10 new)"
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Days back — used for date range context only (default: 7)")
    args = parser.parse_args()

    end   = date.today()
    start = date.today() - timedelta(days=args.days)

    console = Console()
    console.print(f"\n[bold cyan]eCorp Discovery  {start} → {end}[/bold cyan]")
    console.print(f"[dim]Strategy: sequential businessId scan · headless=False (off-screen) · patchright · 1.5 s delay[/dim]\n")

    entities = discover_new_entities(start, end)
    shown    = entities[:10]

    tbl = Table(title=f"Results — {len(shown)} shown of {len(entities)} new entities")
    for col in ("businessId", "Control #", "Entity Name", "Type", "Status", "Formed"):
        tbl.add_column(col, overflow="fold")
    for e in shown:
        tbl.add_row(
            e["control_number"],
            e.get("ecorp_control_number", ""),
            e["entity_name"] or "—",
            e["entity_type"] or "—",
            e["status"] or "—",
            e.get("formation_date") or "—",
        )
    console.print(tbl)
    console.print(f"\n[green]Watermark + screenshots → data/debug/[/green]")
