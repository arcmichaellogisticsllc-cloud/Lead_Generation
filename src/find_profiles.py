"""
Find social media and online listing profiles for a business via targeted Bing searches.

Searches site-by-site using patchright (same pattern as website_finder.py).
Returns the first plausible match per platform — validated by domain.

Platforms searched:
  - Facebook
  - Instagram
  - LinkedIn (company pages only)
  - Yelp
  - Angi
  - Google Maps
"""
from __future__ import annotations

import base64
import logging
import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

logger = logging.getLogger(__name__)

# Platform config: (key, site_filter, path_prefix_required)
# path_prefix_required: URL path must start with this string (or None to skip check)
PLATFORMS = [
    ("facebook",  "site:facebook.com",         "/"),
    ("instagram", "site:instagram.com",         "/"),
    ("linkedin",  "site:linkedin.com/company",  "/company/"),
    ("yelp",      "site:yelp.com/biz",          "/biz/"),
    ("angi",      "site:angi.com",              None),
    ("google_maps", "site:google.com/maps",     "/maps/place/"),
]

PLATFORM_DOMAINS = {
    "facebook":    "facebook.com",
    "instagram":   "instagram.com",
    "linkedin":    "linkedin.com",
    "yelp":        "yelp.com",
    "angi":        "angi.com",
    "google_maps": "google.com",
}

# Facebook/Instagram pages to skip (not business pages)
_EXCLUDE_PATH_FRAGMENTS = {
    "facebook":  ["/help", "/policies", "/groups", "/events", "/pages/create",
                  "/login", "/share", "/sharer", "/dialog", "/legal"],
    "instagram": ["/explore", "/accounts", "/legal"],
    "linkedin":  ["/jobs", "/learning", "/pulse", "/feed", "/legal"],
    "yelp":      ["/writeareview", "/user_details", "/search", "/category"],
    "angi":      [],
    "google_maps": [],
}


def find_profiles(
    entity_name: str,
    city: str | None = None,
    *,
    browser_page=None,
) -> dict[str, str | None]:
    """Search for social/listing profiles across major platforms.

    Returns dict with keys: facebook, instagram, linkedin, yelp, angi, google_maps.
    Values are URLs (str) or None if not found.
    """
    from enrichment.website_finder import _clean_entity_name
    clean_name = _clean_entity_name(entity_name)
    location   = f"{city} Georgia" if city else "Georgia"

    results: dict[str, str | None] = {}
    for platform_key, site_filter, path_prefix in PLATFORMS:
        query  = f'"{clean_name}" {location} {site_filter}'
        urls   = _bing_search(query, browser_page)
        domain = PLATFORM_DOMAINS[platform_key]
        found  = _first_valid(urls, domain, path_prefix, _EXCLUDE_PATH_FRAGMENTS.get(platform_key, []))

        # Retry without quotes if nothing found
        if not found:
            query2 = f"{clean_name} {location} {site_filter}"
            urls2  = _bing_search(query2, browser_page)
            found  = _first_valid(urls2, domain, path_prefix, _EXCLUDE_PATH_FRAGMENTS.get(platform_key, []))

        results[platform_key] = found
        if found:
            logger.info("  %s → %s", platform_key, found)
        else:
            logger.debug("  %s → not found", platform_key)

        time.sleep(0.5)

    return results


# ---------------------------------------------------------------------------
# Bing search (reuses the page object from enrich flow)
# ---------------------------------------------------------------------------

def _bing_search(query: str, page) -> list[str]:
    encoded = quote_plus(query)
    url     = f"https://www.bing.com/search?q={encoded}&form=QBLH"
    try:
        if page is not None:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_selector("li.b_algo h2 a", timeout=8_000)
            except Exception:
                pass
            return _extract_urls(page.content())
        else:
            return _bing_search_new_browser(url)
    except Exception as exc:
        logger.debug("Bing search error (%s): %s", query[:60], exc)
        return []


def _bing_search_new_browser(search_url: str) -> list[str]:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        return []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--window-position=3000,3000"])
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
            page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_selector("li.b_algo h2 a", timeout=8_000)
            except Exception:
                pass
            return _extract_urls(page.content())
        except Exception:
            return []
        finally:
            browser.close()


def _extract_urls(html: str) -> list[str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a[href]")
        if a:
            href = a.get("href", "")
            if href.startswith("http"):
                urls.append(_decode_bing_href(href))
    return urls


def _decode_bing_href(href: str) -> str:
    if "bing.com/ck/a" not in href:
        return href
    try:
        parsed  = urlparse(href)
        u_param = parse_qs(parsed.query).get("u", [""])[0]
        if u_param.startswith("a1"):
            u_param = u_param[2:]
        padding = 4 - len(u_param) % 4
        if padding != 4:
            u_param += "=" * padding
        decoded = base64.urlsafe_b64decode(u_param).decode("utf-8", errors="replace")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass
    return href


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def _first_valid(
    urls: list[str],
    expected_domain: str,
    path_prefix: str | None,
    exclude_fragments: list[str],
) -> str | None:
    for url in urls:
        try:
            p = urlparse(url)
            domain = re.sub(r"^www\.", "", p.netloc.lower())
            if not (domain == expected_domain or domain.endswith("." + expected_domain)):
                continue
            path = p.path.lower()
            # Must have the required path prefix (e.g. /biz/ for Yelp)
            if path_prefix and not path.startswith(path_prefix):
                continue
            # Skip known non-business paths
            if any(frag in path for frag in exclude_fragments):
                continue
            # Must have a meaningful path (not just the homepage)
            if path in ("", "/"):
                continue
            return url
        except Exception:
            continue
    return None
