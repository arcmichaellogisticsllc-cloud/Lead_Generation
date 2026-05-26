"""
Google Places API lookup — finds business phone and website by name + address.

Requires GOOGLE_PLACES_API_KEY env var (Text Search + Place Details enabled).
Falls back gracefully (returns None) when the key is absent or quota exceeded.
"""
from __future__ import annotations

import logging
import os

import httpx

from src.phone_utils import normalize_phone

logger = logging.getLogger(__name__)

_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_DETAILS_URL    = "https://maps.googleapis.com/maps/api/place/details/json"
_TIMEOUT = 10.0


def lookup(entity_name: str, address: str | None = None) -> dict | None:
    """Look up a business via Google Places API.

    Returns a dict with keys: phone, website, formatted_address, place_id.
    Returns None when the API key is missing, quota exceeded, or no result found.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        return None

    query = f"{entity_name} {address}" if address else entity_name

    try:
        # Step 1 — Text Search to get place_id
        r = httpx.get(
            _TEXTSEARCH_URL,
            params={"query": query, "region": "us", "key": api_key},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            logger.debug("Places: no results for %r", query)
            return None

        place_id = results[0].get("place_id")
        if not place_id:
            return None

        # Step 2 — Place Details for phone + website
        r2 = httpx.get(
            _DETAILS_URL,
            params={
                "place_id": place_id,
                "fields": (
                    "formatted_phone_number,international_phone_number,"
                    "website,formatted_address"
                ),
                "key": api_key,
            },
            timeout=_TIMEOUT,
        )
        r2.raise_for_status()
        detail = r2.json().get("result", {})

        raw_phone = (
            detail.get("international_phone_number")
            or detail.get("formatted_phone_number")
        )
        phone = normalize_phone(raw_phone) if raw_phone else None

        logger.info("Places: found %r → place_id=%s phone=%s", query, place_id, phone)
        return {
            "phone":             phone,
            "website":           detail.get("website"),
            "formatted_address": detail.get("formatted_address"),
            "place_id":          place_id,
        }

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            logger.warning("Google Places API key invalid or quota exceeded")
        else:
            logger.warning("Google Places HTTP error for %r: %s", query, exc)
        return None
    except Exception as exc:
        logger.warning("Google Places lookup failed for %r: %s", query, exc)
        return None
