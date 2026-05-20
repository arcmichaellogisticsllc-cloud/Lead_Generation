"""
Scrape entity detail page from eCorp.
RATE LIMIT: caller is responsible for 1.5 s delay between calls.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_URL = "https://ecorp.sos.ga.gov"
DETAIL_URL = f"{BASE_URL}/BusinessSearch/BusinessInformation"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MIN_DELAY = 1.5
SCREENSHOTS_DIR = Path(__file__).parent.parent / "data" / "debug"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_entity_detail(control_number: str) -> dict:
    """Load the eCorp entity detail page and return all extracted fields.

    Returns:
        {
          control_number, entity_name, entity_type, status, formation_date,
          naics_code, principal_office_address,
          registered_agent_name, registered_agent_address,
          organizers: [{name, address, capacity}],
          filing_urls: [{doc_type, url, date}],
        }
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        try:
            result = _scrape_page(page, control_number)
        finally:
            browser.close()

    return result


def scrape_entity_detail_with_page(page, control_number: str) -> dict:
    """Same as scrape_entity_detail but reuses an existing Playwright page.

    Use this when batch-scraping to avoid relaunching the browser each time.
    Caller must enforce MIN_DELAY between calls.
    """
    return _scrape_page(page, control_number)


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def _scrape_page(page, control_number: str) -> dict:
    url = f"{DETAIL_URL}?businessId={control_number}"
    logger.info("Scraping %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(0.5)  # let JS finish rendering

    _screenshot(page, f"detail_{control_number}")

    from bs4 import BeautifulSoup
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    result: dict = {"control_number": control_number}

    # --- Basic entity info ---
    result.update(_extract_basic_info(soup))

    # --- Registered agent ---
    result.update(_extract_registered_agent(soup))

    # --- Organizers / Officers ---
    result["organizers"] = _extract_organizers(soup)

    # --- Filing history + PDF links ---
    result["filing_urls"] = _extract_filing_urls(soup, page)

    logger.debug(
        "Scraped %s: %s organizers, %s filings",
        control_number,
        len(result["organizers"]),
        len(result["filing_urls"]),
    )
    return result


# ---------------------------------------------------------------------------
# Section extractors
# ---------------------------------------------------------------------------

def _extract_basic_info(soup) -> dict:
    """Pull header-level entity fields from the detail page."""
    data: dict = {}

    # eCorp lays these out as label/value pairs in a definition list or table.
    # Try both patterns.

    # Pattern A: <dt>Label</dt><dd>Value</dd>
    dl_pairs = _extract_dl_pairs(soup)
    if dl_pairs:
        data["entity_name"]    = dl_pairs.get("business name") or dl_pairs.get("entity name") or dl_pairs.get("name")
        data["entity_type"]    = dl_pairs.get("business type") or dl_pairs.get("entity type") or dl_pairs.get("type")
        data["status"]         = dl_pairs.get("status") or dl_pairs.get("business status")
        data["formation_date"] = _parse_date(
            dl_pairs.get("date of formation / registration date")
            or dl_pairs.get("date of organization")
            or dl_pairs.get("formation date")
            or dl_pairs.get("filing date")
            or dl_pairs.get("date filed")
        )
        data["naics_code"]     = _extract_naics(dl_pairs.get("naics") or dl_pairs.get("naics code") or "")
        data["principal_office_address"] = (
            dl_pairs.get("principal office address")
            or dl_pairs.get("principal address")
            or dl_pairs.get("business address")
        )

    # Pattern B: two-column <table> rows with label/value
    if not data.get("entity_name"):
        tbl_pairs = _extract_table_label_pairs(soup)
        data["entity_name"]    = tbl_pairs.get("business name") or tbl_pairs.get("entity name")
        data["entity_type"]    = tbl_pairs.get("business type") or tbl_pairs.get("entity type")
        data["status"]         = tbl_pairs.get("status") or tbl_pairs.get("business status")
        data["formation_date"] = _parse_date(
            tbl_pairs.get("date of formation / registration date")
            or tbl_pairs.get("date of organization")
            or tbl_pairs.get("formation date")
            or tbl_pairs.get("filing date")
        )
        data["naics_code"]     = _extract_naics(tbl_pairs.get("naics") or tbl_pairs.get("naics code") or "")
        data["principal_office_address"] = (
            tbl_pairs.get("principal office address")
            or tbl_pairs.get("principal address")
        )

    # Fallback: grab the page <h1> or <h2> as entity name
    if not data.get("entity_name"):
        h = soup.find("h1") or soup.find("h2")
        if h:
            data["entity_name"] = h.get_text(strip=True)

    return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}


def _extract_registered_agent(soup) -> dict:
    """Extract the registered agent block."""
    data: dict = {
        "registered_agent_name": None,
        "registered_agent_address": None,
    }

    # Look for a section heading that contains "Registered Agent"
    section = _find_section(soup, "registered agent")
    if section is None:
        return data

    pairs = _extract_dl_pairs(section) or _extract_table_label_pairs(section)
    if pairs:
        data["registered_agent_name"]    = (
            pairs.get("name") or pairs.get("agent name") or pairs.get("registered agent name")
        )
        data["registered_agent_address"] = (
            pairs.get("address") or pairs.get("agent address") or pairs.get("registered agent address")
        )
    else:
        # Fallback: grab raw text from the section
        lines = [l.strip() for l in section.get_text("\n").splitlines() if l.strip()]
        # First non-header line is typically the agent name
        for line in lines:
            if line.lower() not in ("registered agent", "registered agent information"):
                data["registered_agent_name"] = line
                break

    return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}


def _extract_organizers(soup) -> list[dict]:
    """Extract the organizer/officer/member list."""
    organizers: list[dict] = []

    section = (
        _find_section(soup, "organizer")
        or _find_section(soup, "officer")
        or _find_section(soup, "member")
    )
    if section is None:
        return organizers

    # eCorp typically renders these as a table with Name / Address / Capacity columns
    tables = section.find_all("table") if hasattr(section, "find_all") else []
    for tbl in tables:
        headers = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        for row in tbl.find_all("tr")[1:]:
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            org: dict = {}
            if headers:
                for i, h in enumerate(headers):
                    if i < len(cells):
                        if "name" in h:
                            org["name"] = cells[i]
                        elif "address" in h:
                            org["address"] = cells[i]
                        elif "capacity" in h or "title" in h or "type" in h:
                            org["capacity"] = cells[i]
            else:
                # Positional: Name, Address, Capacity
                org["name"]     = cells[0] if len(cells) > 0 else None
                org["address"]  = cells[1] if len(cells) > 1 else None
                org["capacity"] = cells[2] if len(cells) > 2 else None
            if org.get("name"):
                organizers.append(org)

    return organizers


def _extract_filing_urls(soup, page) -> list[dict]:
    """Extract filing history links (PDFs for Articles, Transmittal, etc.)."""
    filings: list[dict] = []

    section = _find_section(soup, "filing") or _find_section(soup, "document")
    search_root = section if section else soup

    for link in search_root.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)

        # Only follow links that look like document/PDF links
        if not any(kw in href.lower() for kw in (".pdf", "document", "filing", "getdoc", "download")):
            if not any(kw in text.lower() for kw in ("articles", "transmittal", "annual", "pdf", "view")):
                continue

        full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
        doc_type = _classify_doc_type(text, href)

        filings.append({
            "doc_type": doc_type,
            "url":      full_url,
            "label":    text,
        })

    # De-duplicate by URL
    seen: set[str] = set()
    unique: list[dict] = []
    for f in filings:
        if f["url"] not in seen:
            seen.add(f["url"])
            unique.append(f)

    return unique


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _extract_dl_pairs(node) -> dict[str, str]:
    """Extract <dt>/<dd> label-value pairs from a node."""
    pairs: dict[str, str] = {}
    dt_tags = node.find_all("dt")
    for dt in dt_tags:
        label = dt.get_text(strip=True).lower().rstrip(":")
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs[label] = dd.get_text(separator=" ", strip=True)
    return pairs


def _extract_table_label_pairs(node) -> dict[str, str]:
    """Extract label/value pairs from table rows (2-col and 4-col eCorp layout)."""
    pairs: dict[str, str] = {}
    for row in node.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True).lower().rstrip(":")
            value = cells[1].get_text(separator=" ", strip=True)
            if label:
                pairs[label] = value
        if len(cells) >= 4:
            label = cells[2].get_text(strip=True).lower().rstrip(":")
            value = cells[3].get_text(separator=" ", strip=True)
            if label:
                pairs[label] = value
    return pairs


def _find_section(soup, keyword: str):
    """Find a section of the page whose heading contains keyword."""
    for tag in ("h2", "h3", "h4", "h5", "strong", "b", "th", "caption"):
        for el in soup.find_all(tag):
            if keyword.lower() in el.get_text(strip=True).lower():
                # Return the parent container, or siblings up to next heading
                parent = el.find_parent(["div", "section", "table", "fieldset"])
                return parent if parent else el.parent
    return None


def _extract_naics(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"\b(\d{6})\b", raw)
    return m.group(1) if m else (raw.strip() or None)


def _classify_doc_type(text: str, href: str) -> str:
    combined = (text + " " + href).lower()
    if "transmittal" in combined:
        return "transmittal"
    if "articles" in combined:
        return "articles"
    if "annual" in combined:
        return "annual_registration"
    if "amendment" in combined:
        return "amendment"
    return "other"


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
        path = SCREENSHOTS_DIR / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.debug("Screenshot → %s", path)
    except Exception as exc:
        logger.debug("Screenshot failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from rich.console import Console
    from rich import print as rprint

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Checkpoint 3: scrape a single eCorp entity by control number"
    )
    parser.add_argument("control_number", help="eCorp control number to scrape")
    args = parser.parse_args()

    console = Console()
    console.print(f"\n[bold cyan]Scraping control number: {args.control_number}[/bold cyan]\n")

    result = scrape_entity_detail(args.control_number)

    console.print("[bold]Basic Info[/bold]")
    for k, v in result.items():
        if k not in ("organizers", "filing_urls"):
            console.print(f"  [dim]{k}:[/dim] {v}")

    if result.get("organizers"):
        console.print(f"\n[bold]Organizers ({len(result['organizers'])})[/bold]")
        for o in result["organizers"]:
            console.print(f"  {o}")

    if result.get("filing_urls"):
        console.print(f"\n[bold]Filing Documents ({len(result['filing_urls'])})[/bold]")
        for f in result["filing_urls"]:
            console.print(f"  [{f['doc_type']}] {f['label']}  →  {f['url']}")
