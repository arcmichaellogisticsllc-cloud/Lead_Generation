"""
Orchestrate enrichment for a single lead or a batch of leads from the DB.

Enrichment chain per lead:
  1. Find business website via Bing search (patchright).
  2. Detect payment stack via httpx page fetching.
  3. Recompute fit score with payment stack data.
  4. Persist updated fields to the leads table.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from enrichment.website_finder import find_website
from src.classify import is_registered_agent_service
from src.detect_payment_stack import detect_payment_stack
from src.find_profiles import find_profiles
from src.review_scraper import find_review_signals
from src.score import score_lead

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def enrich_lead(lead: dict, browser_page=None, *, find_social: bool = True, find_reviews: bool = True) -> dict:
    """Run website finder + tech detector + social profile search for a single lead dict.

    Args:
        lead: Dict with at minimum ``entity_name``.  Optionally
              ``principal_office_address`` (used to extract city for search).
        browser_page: Reusable patchright Page for Bing search.  If None, a
                      new browser session is launched per call (slower).
        find_social: Also search for social/listing profiles (default True).

    Returns updated dict with website/payment/score/profile fields populated.
    """
    entity_name = lead.get("entity_name") or ""
    city        = _extract_city(lead.get("principal_office_address") or "")

    # registered_agent_is_service — set it if not already present
    ra_is_service = lead.get("registered_agent_is_service")
    if ra_is_service is None:
        ra_name       = lead.get("registered_agent_name") or ""
        ra_is_service = is_registered_agent_service(ra_name)

    # Website search
    logger.info("Enriching: %s (city=%s)", entity_name, city or "?")
    website_url = find_website(entity_name, city, browser_page=browser_page) if entity_name else None

    # Payment-stack detection
    payment_data = detect_payment_stack(website_url)

    # Social / listing profile search
    profiles: dict = {}
    if find_social and entity_name:
        logger.info("  Searching profiles for: %s", entity_name)
        profiles = find_profiles(entity_name, city, browser_page=browser_page)

    # Build enriched copy
    result = dict(lead)
    result["website"]                    = website_url
    result["has_website"]                = payment_data["has_website"]
    result["has_online_payment"]         = payment_data["has_online_payment"]
    result["has_online_booking"]         = payment_data["has_online_booking"]
    result["detected_payment_processor"] = payment_data.get("detected_payment_processor")
    result["detected_vertical_saas"]     = payment_data.get("detected_vertical_saas")
    result["invoice_workflow_signals"]   = payment_data.get("invoice_workflow_signals", False)
    result["registered_agent_is_service"] = ra_is_service

    # Profile URLs
    result["facebook_url"]     = profiles.get("facebook")
    result["instagram_url"]    = profiles.get("instagram")
    result["linkedin_url"]     = profiles.get("linkedin")
    result["yelp_url"]         = profiles.get("yelp")
    result["angi_url"]         = profiles.get("angi")
    result["google_maps_url"]  = profiles.get("google_maps")
    result["profiles_searched"] = 1 if find_social else 0

    # Review signals
    if find_reviews and entity_name:
        logger.info("  Searching reviews for: %s", entity_name)
        rev = find_review_signals(entity_name, city, browser_page=browser_page)
        result["review_snippet"]              = rev.review_snippet or None
        result["review_source"]               = rev.review_source or None
        result["review_has_payment_friction"] = int(rev.payment_friction)
    else:
        result.setdefault("review_snippet", None)
        result.setdefault("review_source", None)
        result.setdefault("review_has_payment_friction", 0)

    # Rescore now that payment data is available
    classification = {
        "tier":              lead.get("tier"),
        "match_source":      lead.get("match_source") or "",
        "industry_category": lead.get("industry_category"),
    }
    scored = score_lead(result, classification, payment_data)
    result["fit_score"]       = scored["fit_score"]
    result["score_breakdown"] = json.dumps(scored["score_breakdown"])
    result["priority"]        = scored["priority"]

    return result


def enrich_batch(
    conn,
    browser_page=None,
    limit: int | None = None,
    min_age_days: int = 14,
) -> tuple[int, int]:
    """Enrich DB leads that don't yet have website/payment data.

    Only processes leads whose ``tier`` is not NULL (i.e. classified and
    eligible) and whose ``has_website`` column is still NULL, OR leads that
    previously had no website but haven't been profile-searched yet and are
    now at least ``min_age_days`` old.

    Brand-new entities (formed within the last ``min_age_days`` days) with no
    website data yet are skipped for expensive enrichment.  They are instead
    scored on filing data only and marked ``has_website=0`` as a provisional
    assumption.  The pipeline will re-enrich them once they age past the
    threshold.

    Args:
        conn: Open SQLite connection.
        browser_page: Reusable patchright Page.  None = launch per call.
        limit: Cap on how many leads to process this run.
        min_age_days: Minimum age (days since formation_date) before a
            has_website=0 / profiles_searched=0 lead is re-enriched.
            Also used as the freshness threshold for skipping new entities.
            Default 14.

    Returns (enriched_count, error_count).
    """
    sql = """
        SELECT * FROM leads
        WHERE (
            has_website IS NULL
            OR (
                has_website = 0
                AND profiles_searched = 0
                AND julianday('now') - julianday(formation_date) >= ?
            )
        )
          AND tier IS NOT NULL
        ORDER BY first_seen DESC
    """
    params: list = [min_age_days]
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    logger.info("Enrichment batch: %d lead(s) to process", len(rows))

    enriched = errors = 0
    for row in rows:
        lead = dict(row)

        # --- Skip enrichment for brand-new entities ---
        formation_date_str = lead.get("formation_date")
        if formation_date_str and lead.get("has_website") is None:
            try:
                from datetime import date as _date
                fd = _date.fromisoformat(str(formation_date_str))
                age_days = (_date.today() - fd).days
                if age_days < min_age_days:
                    logger.info(
                        "Skipping enrichment for fresh entity — scoring on filing data only: %s (age=%d days)",
                        lead.get("entity_name", lead.get("control_number")),
                        age_days,
                    )
                    # Score on filing data only
                    classification = {
                        "tier":              lead.get("tier"),
                        "match_source":      lead.get("match_source") or "",
                        "industry_category": lead.get("industry_category"),
                    }
                    scored = score_lead(lead, classification, payment_data=None)
                    with conn:
                        conn.execute(
                            """
                            UPDATE leads SET
                                has_website=0,
                                fit_score=?, score_breakdown=?, priority=?,
                                last_updated=CURRENT_TIMESTAMP
                            WHERE control_number=?
                            """,
                            (
                                scored.get("fit_score"),
                                json.dumps(scored.get("score_breakdown")),
                                scored.get("priority"),
                                lead["control_number"],
                            ),
                        )
                    continue
            except (ValueError, TypeError):
                pass  # malformed date — fall through to normal enrichment

        try:
            updated = enrich_lead(lead, browser_page=browser_page)
            with conn:
                conn.execute(
                    """
                    UPDATE leads SET
                        website=?, has_website=?, has_online_payment=?,
                        has_online_booking=?, detected_payment_processor=?,
                        detected_vertical_saas=?, invoice_workflow_signals=?,
                        registered_agent_is_service=?,
                        facebook_url=?, instagram_url=?, linkedin_url=?,
                        yelp_url=?, angi_url=?, google_maps_url=?,
                        profiles_searched=?,
                        review_snippet=?, review_source=?,
                        review_has_payment_friction=?,
                        fit_score=?, score_breakdown=?, priority=?,
                        last_updated=CURRENT_TIMESTAMP
                    WHERE control_number=?
                    """,
                    (
                        updated.get("website"),
                        int(updated.get("has_website", False)),
                        int(updated.get("has_online_payment", False)),
                        int(updated.get("has_online_booking", False)),
                        updated.get("detected_payment_processor"),
                        updated.get("detected_vertical_saas"),
                        int(updated.get("invoice_workflow_signals", False)),
                        int(updated.get("registered_agent_is_service", False)),
                        updated.get("facebook_url"),
                        updated.get("instagram_url"),
                        updated.get("linkedin_url"),
                        updated.get("yelp_url"),
                        updated.get("angi_url"),
                        updated.get("google_maps_url"),
                        int(updated.get("profiles_searched", 0)),
                        updated.get("review_snippet"),
                        updated.get("review_source"),
                        int(updated.get("review_has_payment_friction", 0)),
                        updated.get("fit_score"),
                        updated.get("score_breakdown"),
                        updated.get("priority"),
                        lead["control_number"],  # WHERE
                    ),
                )
            enriched += 1
            logger.info(
                "  ✓ %s → score=%s priority=%s",
                lead.get("entity_name", lead["control_number"]),
                updated.get("fit_score"),
                updated.get("priority"),
            )
        except Exception as exc:
            logger.error(
                "Error enriching %s: %s",
                lead.get("entity_name", lead.get("control_number")),
                exc,
            )
            errors += 1

    return enriched, errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_city(address: str) -> str | None:
    """Pull city from a GA address string.

    Handles:
      "123 Main St, Atlanta, GA, 30301, USA"
      "456 Oak Ave Lilburn GA 30047"
    """
    if not address:
        return None
    m = re.search(r",\s*([A-Za-z ]+),\s*GA\b", address)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b([A-Za-z ]{3,25})\s+GA\b", address)
    if m:
        return m.group(1).strip()
    return None
