"""Tests for src/classify.py — run at Checkpoint 2."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.classify import classify_entity, is_registered_agent_service


class TestClassifyByNAICS:
    def test_tier1_naics_match(self):
        # Name has no keyword hits so match_source must be purely "naics"
        result = classify_entity({"entity_name": "Acme Services LLC", "naics_code": "238220"})
        assert result["tier"] == 1
        assert result["match_source"] == "naics"

    def test_tier2_naics_match(self):
        result = classify_entity({"entity_name": "Joe's Auto LLC", "naics_code": "811111"})
        assert result["tier"] == 2
        assert result["match_source"] == "naics"

    def test_tier3_naics_match(self):
        result = classify_entity({"entity_name": "Zen Wellness LLC", "naics_code": "621610"})
        assert result["tier"] == 3
        assert result["match_source"] == "naics"

    def test_unknown_naics_falls_through(self):
        result = classify_entity({"entity_name": "Peach Landscaping LLC", "naics_code": "999999"})
        assert result["tier"] == 1
        assert result["match_source"] == "keyword"


class TestClassifyByKeyword:
    def test_tier1_keyword_hvac(self):
        result = classify_entity({"entity_name": "Atlanta HVAC Solutions LLC", "naics_code": ""})
        assert result["tier"] == 1
        assert result["match_source"] == "keyword"

    def test_tier2_keyword_mechanic(self):
        result = classify_entity({"entity_name": "Riverside Mechanic Shop LLC", "naics_code": ""})
        assert result["tier"] == 2

    def test_tier3_keyword_barber(self):
        result = classify_entity({"entity_name": "The Sharp Barber LLC", "naics_code": ""})
        assert result["tier"] == 3

    def test_no_match(self):
        result = classify_entity({"entity_name": "Consulting Partners Group LLC", "naics_code": ""})
        assert result["tier"] is None
        assert result["match_source"] == "no_match"


class TestDisqualifiers:
    def test_real_estate_disqualified(self):
        result = classify_entity({"entity_name": "Buckhead Real Estate Holdings LLC", "naics_code": "238220"})
        assert result["tier"] is None
        assert result["match_source"] == "disqualified"

    def test_church_disqualified(self):
        result = classify_entity({"entity_name": "New Hope Church LLC", "naics_code": ""})
        assert result["tier"] is None

    def test_investment_disqualified(self):
        result = classify_entity({"entity_name": "Peach Tree Investment Group LLC", "naics_code": ""})
        assert result["tier"] is None


class TestRegisteredAgentService:
    def test_known_ra_service(self):
        assert is_registered_agent_service("Northwest Registered Agent") is True
        assert is_registered_agent_service("ZenBusiness LLC") is True

    def test_unknown_individual(self):
        assert is_registered_agent_service("John Smith") is False
        assert is_registered_agent_service("Mary Johnson, 123 Main St") is False
