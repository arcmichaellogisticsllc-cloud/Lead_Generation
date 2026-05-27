"""Tests for src/score.py — run at Checkpoint 2."""
import pytest
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.score import score_lead, _looks_like_entity


FRESH_LLC = {
    "entity_name": "Peach State Plumbing LLC",
    "entity_type": "LLC",
    "formation_date": str(date.today()),
    "organizer_name": "John Smith",
    "registered_agent_is_service": False,
    "naics_code": "238220",
    "filer_phone": "(770) 555-0123",
}
TIER1_NAICS = {"tier": 1, "match_source": "naics", "industry_category": "Plumbing"}
NO_WEBSITE = {"has_website": False, "has_online_payment": False, "detected_payment_processor": None, "detected_vertical_saas": None, "invoice_workflow_signals": None}


class TestScoreModel:
    def test_hot_lead(self):
        result = score_lead(FRESH_LLC, TIER1_NAICS, NO_WEBSITE)
        assert result["priority"] == "HOT"
        assert result["fit_score"] >= 80

    def test_skip_lead_disqualified(self):
        entity = {**FRESH_LLC, "naics_code": ""}
        classification = {"tier": None, "match_source": "disqualified", "industry_category": None}
        result = score_lead(entity, classification, None)
        assert result["fit_score"] == 0
        assert result["priority"] == "SKIP"

    def test_score_capped_at_100(self):
        result = score_lead(FRESH_LLC, TIER1_NAICS, NO_WEBSITE)
        assert result["fit_score"] <= 100

    def test_score_minimum_zero(self):
        entity = {**FRESH_LLC, "entity_type": "Corp", "registered_agent_is_service": True}
        payment = {
            "has_website": True,
            "has_online_payment": True,
            "detected_payment_processor": "square",
            "detected_vertical_saas": "servicetitan",
            "invoice_workflow_signals": None,
        }
        result = score_lead(entity, TIER1_NAICS, payment)
        assert result["fit_score"] >= 0

    def test_payment_processor_reduces_score(self):
        no_payment = score_lead(FRESH_LLC, TIER1_NAICS, NO_WEBSITE)
        with_square = score_lead(
            FRESH_LLC,
            TIER1_NAICS,
            {**NO_WEBSITE, "has_website": True, "detected_payment_processor": "square"},
        )
        assert with_square["fit_score"] < no_payment["fit_score"]

    def test_score_breakdown_present(self):
        result = score_lead(FRESH_LLC, TIER1_NAICS, NO_WEBSITE)
        assert isinstance(result["score_breakdown"], dict)
        assert "industry" in result["score_breakdown"]

    def test_tier2_keyword_score(self):
        entity = {
            "entity_name": "Metro Auto Repair LLC",
            "entity_type": "LLC",
            "formation_date": str(date.today()),
            "organizer_name": "Bob Jones",
            "registered_agent_is_service": False,
            "naics_code": "",
        }
        classification = {"tier": 2, "match_source": "keyword", "industry_category": "auto repair"}
        result = score_lead(entity, classification, NO_WEBSITE)
        assert result["score_breakdown"]["industry"] == 18  # tier_2_keyword_match
        assert result["priority"] in ("HOT", "WARM", "COLD")


class TestLooksLikeEntity:
    def test_llc_is_entity(self):
        assert _looks_like_entity("Smith Auto Group LLC") is True

    def test_individual_name(self):
        assert _looks_like_entity("John Smith") is False

    def test_corp_is_entity(self):
        assert _looks_like_entity("Big Corp Inc") is True
