"""
Detect payment stack fingerprints from a business website.

Strategy:
  1. Fetch homepage HTML via httpx (10 s timeout, follow redirects).
  2. Optionally fetch /contact and /services for invoice-language signals.
  3. Scan the combined HTML for fingerprints defined in
     config/payment_stack_fingerprints.yaml.
  4. Return a structured dict consumed by score.py.

Returns {
  has_website, has_online_payment, has_online_booking,
  detected_payment_processor, detected_vertical_saas,
  invoice_workflow_signals, website_url, pages_fetched,
}
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_FINGERPRINTS: dict | None = None

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

EXTRA_PATHS = ["/contact", "/services", "/about"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_payment_stack(website_url: str | None) -> dict:
    """Fetch pages from website_url and return payment-stack fingerprint data.

    If website_url is None or empty, returns a no-website result immediately.
    """
    empty: dict[str, Any] = {
        "website_url":                website_url,
        "has_website":                False,
        "has_online_payment":         False,
        "has_online_booking":         False,
        "detected_payment_processor": None,
        "detected_vertical_saas":     None,
        "invoice_workflow_signals":   False,
        "pages_fetched":              0,
    }

    if not website_url:
        return empty

    pages = _fetch_pages(website_url)
    if not pages:
        return empty  # unreachable host — treat as no website

    result = dict(empty)
    result["has_website"]   = True
    result["pages_fetched"] = len(pages)

    combined = "\n".join(pages)
    cfg      = _load_config()

    _detect_processors(result, combined, cfg)
    _detect_vertical_saas(result, combined, cfg)
    _detect_invoice_signals(result, combined, cfg)
    _detect_booking(result, combined)

    return result


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _detect_processors(result: dict, html: str, cfg: dict) -> None:
    for name, data in cfg.get("payment_processors", {}).items():
        for fp in data.get("fingerprints", []):
            if fp.lower() in html.lower():
                result["detected_payment_processor"] = name
                result["has_online_payment"]         = True
                logger.debug("Payment processor: %s (%s)", name, fp)
                return


def _detect_vertical_saas(result: dict, html: str, cfg: dict) -> None:
    for name, data in cfg.get("vertical_saas", {}).items():
        for fp in data.get("fingerprints", []):
            if fp.lower() in html.lower():
                result["detected_vertical_saas"] = name
                result["has_online_booking"]     = True
                logger.debug("Vertical SaaS: %s (%s)", name, fp)
                return


def _detect_invoice_signals(result: dict, html: str, cfg: dict) -> None:
    fps = (
        cfg.get("opportunity_signals", {})
           .get("invoice_language", {})
           .get("fingerprints", [])
    )
    html_lower = html.lower()
    for fp in fps:
        if fp.lower() in html_lower:
            result["invoice_workflow_signals"] = True
            logger.debug("Invoice signal: %r", fp)
            return


def _detect_booking(result: dict, html: str) -> None:
    if result["has_online_booking"]:
        return
    kws = (
        "book now", "book online", "schedule appointment",
        "book an appointment", "request appointment",
        "schedule online", "online booking",
    )
    html_lower = html.lower()
    for kw in kws:
        if kw in html_lower:
            result["has_online_booking"] = True
            return


# ---------------------------------------------------------------------------
# HTTP fetcher
# ---------------------------------------------------------------------------

def _fetch_pages(base_url: str) -> list[str]:
    """Fetch homepage + optional sub-paths. Returns list of HTML strings."""
    base_url = base_url.rstrip("/")
    paths    = [""] + EXTRA_PATHS
    pages:   list[str] = []

    with httpx.Client(
        timeout=10,
        follow_redirects=True,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
        verify=False,  # tolerate self-signed/expired certs on small-biz sites
    ) as client:
        for path in paths:
            url = base_url + path
            try:
                resp = client.get(url)
                ct = resp.headers.get("content-type", "")
                if resp.status_code == 200 and "text/html" in ct:
                    pages.append(resp.text)
                    logger.debug("Fetched %s (%d chars)", url, len(resp.text))
                else:
                    logger.debug("Skipping %s (status=%d)", url, resp.status_code)
            except Exception as exc:
                if path == "":
                    logger.info("Homepage unreachable %s: %s", url, exc)
                    return []
                logger.debug("Sub-path %s: %s", url, exc)

    return pages


# ---------------------------------------------------------------------------
# Config loader (lazy, cached)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    global _FINGERPRINTS
    if _FINGERPRINTS is None:
        _FINGERPRINTS = yaml.safe_load(
            (_CONFIG_DIR / "payment_stack_fingerprints.yaml").read_text()
        )
    return _FINGERPRINTS


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

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from enrichment.website_finder import find_website

    parser = argparse.ArgumentParser(
        description="Checkpoint 5: detect payment stack for given URLs or entity names"
    )
    parser.add_argument("--urls", nargs="+", help="URLs to analyze directly")
    args = parser.parse_args()

    # Default test set: known-website businesses + brand-new entities + edge cases
    DEFAULT_TESTS: list[tuple[str, str | None]] = [
        # Direct URL probes (no search needed)
        ("squareup.com (known Square)",    "https://squareup.com"),
        ("stripe.com (known Stripe)",      "https://stripe.com"),
        ("example.com (no payment)",       "https://example.com"),
        ("None / no website",              "skip"),
        # Search-based: well-established GA businesses that definitely have websites
        ("Roto-Rooter Atlanta GA",         None),
        ("Merry Maids Atlanta GA",         None),
        # Brand-new 2026 LLC filings (likely no web presence yet)
        ("1 of One Plumbing Atl (Lilburn GA)", None),
        ("Edens Earthworks (Rome GA)",     None),
    ]

    if args.urls:
        tests: list[tuple[str, str | None]] = [(u, u) for u in args.urls]
    else:
        tests = DEFAULT_TESTS

    console = Console()
    console.print(f"\n[bold cyan]Payment Stack Detector — {len(tests)} targets[/bold cyan]\n")

    tbl = Table(title="Results")
    for col in ("Label", "URL", "Website?", "Has Payment", "Processor", "SaaS", "Invoice", "Pages"):
        tbl.add_column(col, overflow="fold")

    # "skip" sentinel = no website, no search
    tests = [(lbl, None if url == "skip" else url) for lbl, url in tests]
    needs_search = any(url is None for _, url in tests)
    _search_page = None

    def _run_tests(search_page_arg):
        for label, url in tests:
            if url is None:
                search_name = label.split(" (")[0]
                city_m = __import__("re").search(r"\((.+?) GA\)", label)
                city   = city_m.group(1) if city_m else None
                console.print(f"[dim]Searching: {search_name!r} in {city or 'GA'}[/dim]")
                url = find_website(search_name, city, browser_page=search_page_arg)
                console.print(f"[dim]  → {url or 'not found'}[/dim]")

            console.print(f"[dim]Analyzing: {url}[/dim]")
            data = detect_payment_stack(url)

            tbl.add_row(
                label,
                (data.get("website_url") or "—")[:50],
                "✓" if data.get("has_website")        else "✗",
                "✓" if data.get("has_online_payment") else "✗",
                data.get("detected_payment_processor") or "—",
                data.get("detected_vertical_saas")     or "—",
                "✓" if data.get("invoice_workflow_signals") else "✗",
                str(data.get("pages_fetched", 0)),
            )

    if needs_search:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False, args=["--window-position=3000,3000"]
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            _search_page = ctx.new_page()
            _run_tests(_search_page)
            browser.close()
    else:
        _run_tests(None)

    console.print(tbl)
