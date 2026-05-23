"""
Compute fit score 0-100 for each lead.
Implemented at Checkpoint 2.
"""
from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path
import yaml

_CONFIG_DIR = Path(__file__).parent.parent / "config"

_WEIGHTS: dict = yaml.safe_load((_CONFIG_DIR / "scoring_weights.yaml").read_text())

def _build_payment_weights() -> dict:
    cfg = yaml.safe_load((_CONFIG_DIR / "payment_stack_fingerprints.yaml").read_text())
    weights = {}
    for category in ("payment_processors", "vertical_saas"):
        for name, data in cfg.get(category, {}).items():
            weights[name] = data["weight"]
    for name, data in cfg.get("opportunity_signals", {}).items():
        weights[name] = data["weight"]
    return weights

_PAYMENT_WEIGHTS: dict = _build_payment_weights()


def _load_weights() -> dict:
    return _WEIGHTS


def _load_payment_weights() -> dict:
    return _PAYMENT_WEIGHTS


def score_lead(
    entity_data: dict,
    classification: dict,
    payment_stack_data: dict | None = None,
) -> dict:
    """Return {fit_score, score_breakdown, priority}."""
    weights = _load_weights()
    payment_weights = _load_payment_weights()
    breakdown: dict[str, int] = {}
    total = 0

    # --- Industry score ---
    tier = classification.get("tier")

    # Disqualified entities get zero — don't award owner-operator or recency points
    if tier is None:
        return {"fit_score": 0, "score_breakdown": {"industry": 0}, "priority": "SKIP"}
    match_source = classification.get("match_source", "")
    if tier == 1:
        key = "tier_1_naics_match" if "naics" in match_source else "tier_1_keyword_match"
        pts = weights["industry"][key]
    elif tier == 2:
        key = "tier_2_naics_match" if "naics" in match_source else "tier_2_keyword_match"
        pts = weights["industry"][key]
    elif tier == 3:
        key = "tier_3_naics_match" if "naics" in match_source else "tier_3_keyword_match"
        pts = weights["industry"][key]
    else:
        pts = 0
    breakdown["industry"] = pts
    total += pts

    # --- Owner-operator signals ---
    oo = weights["owner_operator_signals"]

    ra_is_service = entity_data.get("registered_agent_is_service", False)
    if not ra_is_service:
        breakdown["residential_registered_agent"] = oo["residential_registered_agent"]
        total += oo["residential_registered_agent"]
    else:
        breakdown["residential_registered_agent"] = 0

    organizer = entity_data.get("organizer_name") or ""
    if organizer and not _looks_like_entity(organizer):
        breakdown["individual_organizer"] = oo["individual_organizer"]
        total += oo["individual_organizer"]
    else:
        breakdown["individual_organizer"] = 0

    entity_type = (entity_data.get("entity_type") or "").upper()
    if "LLC" in entity_type:
        breakdown["llc_not_inc"] = oo["llc_not_inc"]
        total += oo["llc_not_inc"]
    else:
        breakdown["llc_not_inc"] = 0

    # --- Recency ---
    recency = weights["recency"]
    formation_date = entity_data.get("formation_date")
    if formation_date:
        if isinstance(formation_date, str):
            try:
                formation_date = date.fromisoformat(formation_date)
            except ValueError:
                formation_date = None
    if formation_date:
        today = date.today()
        days_old = (today - formation_date).days
        if days_old <= 14:
            breakdown["within_14_days"] = recency["within_14_days"]
            total += recency["within_14_days"]
        else:
            breakdown["within_14_days"] = 0
        if days_old <= 7:
            breakdown["within_7_days"] = recency["within_7_days"]
            total += recency["within_7_days"]
        else:
            breakdown["within_7_days"] = 0
    else:
        breakdown["within_14_days"] = 0
        breakdown["within_7_days"] = 0

    # --- Payment stack adjustments ---
    if payment_stack_data:
        detected_processor = payment_stack_data.get("detected_payment_processor")
        if detected_processor and detected_processor in payment_weights:
            pts = payment_weights[detected_processor]
            breakdown[f"payment_{detected_processor}"] = pts
            total += pts

        detected_saas = payment_stack_data.get("detected_vertical_saas")
        if detected_saas and detected_saas in payment_weights:
            pts = payment_weights[detected_saas]
            breakdown[f"saas_{detected_saas}"] = pts
            total += pts

        if not payment_stack_data.get("has_website"):
            pts = payment_weights.get("no_website", 0)
            breakdown["no_website"] = pts
            total += pts
        if not payment_stack_data.get("has_online_payment"):
            pts = payment_weights.get("no_online_payment_detected", 0)
            breakdown["no_online_payment_detected"] = pts
            total += pts

        if payment_stack_data.get("invoice_workflow_signals"):
            pts = payment_weights.get("invoice_language", 0)
            breakdown["invoice_language"] = pts
            total += pts

    fit_score = max(0, min(100, total))

    thresholds = weights["priority_tiers"]
    if fit_score >= thresholds["hot_threshold"]:
        priority = "HOT"
    elif fit_score >= thresholds["warm_threshold"]:
        priority = "WARM"
    elif fit_score >= thresholds["cold_threshold"]:
        priority = "COLD"
    else:
        priority = "SKIP"

    return {
        "fit_score": fit_score,
        "score_breakdown": breakdown,
        "priority": priority,
    }


def _looks_like_entity(name: str) -> bool:
    entity_suffixes = ("llc", "inc", "corp", "ltd", "lp", "llp", "pllc", "pc", "co.")
    name_lower = name.lower()
    return any(name_lower.endswith(s) or f" {s} " in name_lower for s in entity_suffixes)


if __name__ == "__main__":
    test_cases = [
        {
            "label": "HOT — Tier 1 NAICS, individual organizer, fresh LLC, no website",
            "entity": {
                "entity_name": "Peach State Plumbing LLC",
                "entity_type": "LLC",
                "formation_date": str(date.today()),
                "organizer_name": "John Smith",
                "registered_agent_is_service": False,
                "naics_code": "238220",
            },
            "classification": {"tier": 1, "match_source": "naics", "industry_category": "Plumbing"},
            "payment": {"has_website": False, "has_online_payment": False},
        },
        {
            "label": "WARM — Tier 2 keyword, RA service, older",
            "entity": {
                "entity_name": "Metro Auto Repair LLC",
                "entity_type": "LLC",
                "formation_date": "2025-12-01",
                "organizer_name": "Smith Auto Group LLC",
                "registered_agent_is_service": True,
                "naics_code": "",
            },
            "classification": {"tier": 2, "match_source": "keyword", "industry_category": "auto repair"},
            "payment": {"has_website": True, "has_online_payment": False},
        },
        {
            "label": "SKIP — disqualified / real estate",
            "entity": {
                "entity_name": "Buckhead Real Estate Holdings LLC",
                "entity_type": "LLC",
                "formation_date": str(date.today()),
                "organizer_name": "Jane Doe",
                "registered_agent_is_service": False,
                "naics_code": "",
            },
            "classification": {"tier": None, "match_source": "disqualified", "industry_category": None},
            "payment": None,
        },
    ]

    for tc in test_cases:
        result = score_lead(tc["entity"], tc["classification"], tc["payment"])
        print(f"\n{tc['label']}")
        print(f"  Score: {result['fit_score']}  Priority: {result['priority']}")
        print(f"  Breakdown: {result['score_breakdown']}")
