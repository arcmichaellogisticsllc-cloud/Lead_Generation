"""
Find a business's website via Bing search using patchright (Cloudflare-bypass
patched Playwright). Runs a headless browser off-screen to handle JS rendering
and bot challenges that defeat plain httpx requests.

RATE LIMIT: ~2 s between searches (enforced by browser navigation time).
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger(__name__)

# Domains that are directories, aggregators, or social media — NOT the business's own site
AGGREGATOR_DOMAINS = {
    "yelp.com", "bbb.org", "manta.com", "yellowpages.com",
    "whitepages.com", "mapquest.com", "superpages.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "pinterest.com", "tiktok.com",
    "google.com", "google.co", "bing.com", "yahoo.com",
    "angi.com", "angieslist.com", "homeadvisor.com", "thumbtack.com",
    "houzz.com", "buildzoom.com", "bark.com", "porch.com",
    "nextdoor.com", "glassdoor.com", "indeed.com", "ziprecruiter.com",
    "opencorporates.com", "bizapedia.com", "dnb.com", "bloomberg.com",
    "bizbuysell.com", "crunchbase.com", "zoominfo.com",
    "sosfilings.com", "corpinfo.com", "lawinsider.com",
    "duckduckgo.com", "reddit.com", "wikipedia.org",
    "tripadvisor.com", "foursquare.com", "maps.apple.com",
    "microsoft.com", "msn.com",
}


def find_website(
    entity_name: str,
    city: str | None = None,
    *,
    browser_page=None,
) -> str | None:
    """Search Bing for the business's own website.

    Strips common suffixes (LLC, Inc, Corp) before searching.
    Falls back gracefully if Bing blocks or returns no usable results.

    Args:
        entity_name: Business name (e.g. "Acme Plumbing LLC")
        city: Optional city name for geographic context
        browser_page: Optional patchright Page object to reuse. If None,
                      a new browser context is launched (slower but standalone).

    Returns the first non-aggregator HTTP(S) URL, or None.
    """
    clean_name = _clean_entity_name(entity_name)
    location   = f"{city} Georgia" if city else "Georgia"
    query      = f'"{clean_name}" {location}'

    logger.debug("Website search: %s", query)

    if browser_page is not None:
        urls = _bing_search_with_page(query, browser_page)
    else:
        urls = _bing_search_new_browser(query)

    for url in urls:
        if _is_business_url(url):
            logger.info("Found website for %r: %s", entity_name, url)
            return url

    # Retry without quotes if quoted search found nothing
    if not urls:
        query2 = f"{clean_name} {location}"
        if browser_page is not None:
            urls2 = _bing_search_with_page(query2, browser_page)
        else:
            urls2 = _bing_search_new_browser(query2)
        for url in urls2:
            if _is_business_url(url):
                logger.info("Found website (unquoted) for %r: %s", entity_name, url)
                return url

    logger.info("No website found for %r", entity_name)
    return None


# ---------------------------------------------------------------------------
# Bing search helpers
# ---------------------------------------------------------------------------

def _bing_search_with_page(query: str, page) -> list[str]:
    """Search Bing using an existing patchright page object."""
    encoded = quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&form=QBLH"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        # Wait for organic results to render
        try:
            page.wait_for_selector("li.b_algo h2 a", timeout=8_000)
        except Exception:
            pass
        return _extract_bing_urls(page.content())
    except Exception as exc:
        logger.debug("Bing search error (page): %s", exc)
        return []


def _bing_search_new_browser(query: str) -> list[str]:
    """Launch a standalone patchright browser, search Bing, return URLs."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        logger.warning("patchright not installed — website search unavailable")
        return []

    encoded = quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&form=QBLH"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--window-position=3000,3000"],
        )
        ctx  = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_selector("li.b_algo h2 a", timeout=8_000)
            except Exception:
                pass
            return _extract_bing_urls(page.content())
        except Exception as exc:
            logger.debug("Bing search error (new browser): %s", exc)
            return []
        finally:
            browser.close()


def _extract_bing_urls(html: str) -> list[str]:
    """Parse Bing result HTML and extract organic result URLs (decoded from redirects)."""
    import base64
    from bs4 import BeautifulSoup
    from urllib.parse import parse_qs

    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []

    def _decode_bing_href(href: str) -> str:
        """Decode Bing click-tracking redirect to actual destination URL.

        Bing encodes the real URL in the ?u= parameter as base64 with an "a1" prefix.
        e.g. u=a1aHR0cHM6Ly93d3cucm90b3Jvb3Rlci5jb20v → https://www.rotorooter.com/
        """
        if "bing.com/ck/a" not in href:
            return href
        try:
            parsed = urlparse(href)
            u_param = parse_qs(parsed.query).get("u", [""])[0]
            if u_param.startswith("a1"):
                u_param = u_param[2:]  # strip "a1" prefix
            # add padding if needed
            padding = 4 - len(u_param) % 4
            if padding != 4:
                u_param += "=" * padding
            decoded = base64.urlsafe_b64decode(u_param).decode("utf-8", errors="replace")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
        return href

    # Organic results: <li class="b_algo"><h2><a href="...">
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a[href]")
        if a:
            href = a.get("href", "")
            if href.startswith("http"):
                urls.append(_decode_bing_href(href))

    # Fallback: any <cite> elements contain display URLs
    if not urls:
        for cite in soup.select("li.b_algo cite"):
            text = cite.get_text(strip=True)
            if text and not text.startswith("http"):
                text = "https://" + text.split("/")[0]
            if text.startswith("http"):
                urls.append(text)

    logger.debug("Bing extracted %d URLs", len(urls))
    return urls


# ---------------------------------------------------------------------------
# URL quality helpers
# ---------------------------------------------------------------------------

def _is_business_url(url: str) -> bool:
    """Return True if the URL looks like a real business website (not aggregator)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        domain = parsed.netloc.lower()
        domain = re.sub(r":\d+$", "", domain)
        domain = re.sub(r"^www\.", "", domain)
        for agg in AGGREGATOR_DOMAINS:
            if domain == agg or domain.endswith("." + agg):
                return False
        return bool(domain)
    except Exception:
        return False


def _clean_entity_name(name: str) -> str:
    """Remove legal suffixes and punctuation for cleaner search."""
    suffixes = r"\b(?:LLC|Inc|Corp|Ltd|LP|LLP|PLLC|PC|Co\.?)\b\.?"
    cleaned = re.sub(suffixes, "", name, flags=re.IGNORECASE).strip(" ,.")
    return cleaned
