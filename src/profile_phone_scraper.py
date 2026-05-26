"""
Extract a business phone number from a profile or business website URL.

Strategy per source:
  1. tel: href links — most reliable, usually in <a href="tel:+1...">
  2. JSON-LD structured data — business schema with "telephone" field
  3. Text regex fallback — scan visible text for any 10-digit pattern

Google Maps URLs are skipped here (JS-only); use the Places API for those.
Facebook URLs are skipped (login wall).
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

import httpx

from src.phone_utils import extract_phones_from_text, normalize_phone

logger = logging.getLogger(__name__)

_TIMEOUT = 12.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Domains we won't scrape (JS-only or login-walled)
_SKIP_DOMAINS = {"google.com", "facebook.com", "instagram.com"}


def scrape_phone(url: str) -> str | None:
    """Try to extract a business phone from the given URL. Returns E.164 or None."""
    if not url:
        return None
    try:
        netloc = urlparse(url).netloc.lower()
        domain = re.sub(r"^www\.", "", netloc)
        # Check against skip list (exact + subdomain)
        if any(domain == d or domain.endswith("." + d) for d in _SKIP_DOMAINS):
            return None
    except Exception:
        return None

    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        html = resp.text
    except httpx.SSLError:
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT,
                             follow_redirects=True, verify=False)
            html = resp.text
        except Exception as exc:
            logger.debug("Failed to fetch %s: %s", url, exc)
            return None
    except Exception as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
        return None

    return _extract_phone(html)


def _extract_phone(html: str) -> str | None:
    phone = _from_tel_links(html)
    if phone:
        return phone
    phone = _from_json_ld(html)
    if phone:
        return phone
    phones = extract_phones_from_text(html)
    return phones[0] if phones else None


def _from_tel_links(html: str) -> str | None:
    """Parse <a href="tel:..."> links — most reliable source."""
    for match in re.finditer(r'href=["\']tel:([^"\'>\s]+)', html, re.IGNORECASE):
        phone = normalize_phone(match.group(1))
        if phone:
            return phone
    return None


def _from_json_ld(html: str) -> str | None:
    """Extract telephone from JSON-LD structured data blocks."""
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.group(1))
            # JSON-LD can be a single object, an array, or a @graph wrapper
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and "@graph" in data:
                items = data["@graph"]
            else:
                items = [data]
            for item in items:
                raw = item.get("telephone") or item.get("phone")
                if raw:
                    phone = normalize_phone(str(raw))
                    if phone:
                        return phone
        except Exception:
            continue
    return None


def scrape_best_phone(
    website: str | None,
    yelp_url: str | None,
    angi_url: str | None,
) -> str | None:
    """Try each source in priority order and return the first phone found."""
    for url in (website, yelp_url, angi_url):
        if not url:
            continue
        phone = scrape_phone(url)
        if phone:
            logger.info("  Phone scraped from %s: %s", url, phone)
            return phone
    return None
