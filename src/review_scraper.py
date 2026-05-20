"""
Find review snippets for a business via Bing search.

Searches for "[entity name] [city] reviews" and extracts:
- Number of reviews / rating mention (if in snippets)
- Any snippet that mentions payment-related friction

Returns a ReviewResult with the most useful text found.
Used during enrichment to personalize outreach copy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

PAYMENT_KEYWORDS = [
    "check", "cash", "cash only", "card", "credit card", "debit",
    "payment", "pay", "invoice", "billing", "venmo", "zelle",
    "square", "charged", "bill", "receipt",
]

PAIN_KEYWORDS = [
    "didn't show", "no show", "late", "overpriced", "expensive",
    "never called back", "unprofessional", "poor communication",
    "couldn't pay", "wouldn't accept",
]


@dataclass
class ReviewResult:
    has_reviews: bool = False
    payment_friction: bool = False
    review_snippet: str = ""
    review_source: str = ""
    rating_text: str = ""


def find_review_signals(
    entity_name: str,
    city: str | None = None,
    *,
    browser_page=None,
) -> ReviewResult:
    """Search Bing for reviews of this business and extract payment friction signals."""
    from enrichment.website_finder import _clean_entity_name, _extract_bing_urls
    clean = _clean_entity_name(entity_name)
    location = f"{city} Georgia" if city else "Georgia"
    query = f'"{clean}" {location} reviews'

    html = _bing_html(query, browser_page)
    if not html:
        return ReviewResult()

    return _parse_review_signals(html)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bing_html(query: str, page) -> str:
    encoded = quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&form=QBLH"
    try:
        if page is not None:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_selector("li.b_algo", timeout=8_000)
            except Exception:
                pass
            return page.content()
        else:
            return _fetch_new_browser(url)
    except Exception as exc:
        logger.debug("Review search error: %s", exc)
        return ""


def _fetch_new_browser(search_url: str) -> str:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        return ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--window-position=3000,3000"])
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_selector("li.b_algo", timeout=8_000)
            except Exception:
                pass
            return page.content()
        except Exception:
            return ""
        finally:
            browser.close()


def _parse_review_signals(html: str) -> ReviewResult:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ReviewResult()

    soup = BeautifulSoup(html, "lxml")
    result = ReviewResult()

    # Collect all snippet texts from organic results
    snippets: list[tuple[str, str]] = []  # (text, source_url)
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a[href]")
        href = a.get("href", "") if a else ""
        caption = li.select_one(".b_caption p, .b_snippetBigText")
        if caption:
            snippets.append((caption.get_text(" ", strip=True), href))

    # Also check the rich snippet / knowledge panel area
    for rich in soup.select(".b_rich, .b_vlist2col li"):
        text = rich.get_text(" ", strip=True)
        if text:
            snippets.append((text, ""))

    for text, src in snippets:
        text_lower = text.lower()

        # Detect review presence
        if re.search(r"\d+\s+review|rated\s+[\d.]+|stars?\s+·|\d+\s+ratings?", text_lower):
            result.has_reviews = True
            result.rating_text = text[:120]

        # Detect payment friction
        for kw in PAYMENT_KEYWORDS:
            if kw in text_lower:
                result.has_reviews = True
                result.payment_friction = True
                # Keep the most relevant snippet (shortest that contains the keyword)
                if not result.review_snippet or len(text) < len(result.review_snippet):
                    result.review_snippet = text[:300]
                    result.review_source = src
                break

    # Fall back to any review-site snippet if we found reviews but no payment mention
    if result.has_reviews and not result.review_snippet and snippets:
        for text, src in snippets:
            if any(s in text.lower() for s in ["yelp", "google", "review", "rated"]):
                result.review_snippet = text[:300]
                result.review_source = src
                break

    return result
