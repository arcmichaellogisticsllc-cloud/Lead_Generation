"""Tests for src/extract_pdf.py — run at Checkpoint 4."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.extract_pdf import extract_filing_data, _parse_ra_standard, _parse_ra_wrapped

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"


# ---------------------------------------------------------------------------
# Unit tests — RA parsers
# ---------------------------------------------------------------------------

class TestRaStandard:
    def test_simple_name_addr_county(self):
        r = _parse_ra_standard("Dailton Diaz 4485 Lawrenceville Hwy, Lilburn, GA, 30047, USA Gwinnett")
        assert r["registered_agent_name"] == "Dailton Diaz"
        assert "4485 Lawrenceville" in r["registered_agent_address"]
        assert "Gwinnett" not in r["registered_agent_address"]
        assert r["registered_agent_county"] == "Gwinnett"

    def test_three_word_name(self):
        r = _parse_ra_standard("Maddison Alexis Cope 18 Canterbury Circle, Savannah, GA, 31419, USA Chatham")
        assert r["registered_agent_name"] == "Maddison Alexis Cope"
        assert r["registered_agent_county"] == "Chatham"

    def test_alphanumeric_address_number(self):
        r = _parse_ra_standard("Sawyer Edens 3351A Kingston Hwy ne, Rome, GA, 30161, USA Floyd")
        assert r["registered_agent_name"] == "Sawyer Edens"
        assert "3351A" in r["registered_agent_address"]
        assert "Floyd" not in r["registered_agent_address"]
        assert r["registered_agent_county"] == "Floyd"


class TestRaWrapped:
    def test_yoon_style_address_before_name(self):
        lines = [
            "1796 SATELLITE BLVD, UNIT 1306, DULUTH, GA, 30097,",
            "kyung hee yoon Gwinnett",
            "USA",
        ]
        r = _parse_ra_wrapped(lines)
        assert r["registered_agent_name"] == "kyung hee yoon"
        assert "1796 SATELLITE" in r["registered_agent_address"]
        assert r["registered_agent_county"] == "Gwinnett"
        assert "USA" in r["registered_agent_address"]


# ---------------------------------------------------------------------------
# Integration tests — real PDFs (skipped if PDFs not present)
# ---------------------------------------------------------------------------

def _pdf(ctrl):
    return RAW_DIR / f"{ctrl}_formation.pdf"

@pytest.mark.skipif(not _pdf("26041493").exists(), reason="PDF not downloaded")
class TestPlumbingPDF:
    def setup_method(self):
        self.data = extract_filing_data(_pdf("26041493"))

    def test_entity_name(self):
        assert "Plumbing" in self.data["entity_name"]

    def test_control_number(self):
        assert self.data["control_number"] == "26041493"

    def test_organizer_name(self):
        assert self.data["organizer_name"] == "Dailton Diaz"

    def test_principal_address(self):
        assert "Lawrenceville" in self.data["principal_office_address"]
        assert "GA" in self.data["principal_office_address"]

    def test_ra_name(self):
        assert self.data["registered_agent_name"] == "Dailton Diaz"

    def test_ra_county(self):
        assert self.data["registered_agent_county"] == "Gwinnett"

    def test_formation_date(self):
        assert self.data["effective_date"] == "02/17/2026"

    def test_no_email_phone(self):
        assert self.data["filer_email"] is None
        assert self.data["filer_phone"] is None


@pytest.mark.skipif(not _pdf("26041386").exists(), reason="PDF not downloaded")
class TestYoonPDF:
    def setup_method(self):
        self.data = extract_filing_data(_pdf("26041386"))

    def test_entity_name(self):
        assert "YOON" in self.data["entity_name"]

    def test_ra_name_wrapped_column(self):
        assert self.data["registered_agent_name"] == "kyung hee yoon"

    def test_ra_address_has_no_name(self):
        assert "kyung" not in self.data["registered_agent_address"].lower()

    def test_organizer(self):
        assert self.data["organizer_name"] == "kyung hee yoon"


@pytest.mark.skipif(not _pdf("26047327").exists(), reason="PDF not downloaded")
class TestEdensPDF:
    def setup_method(self):
        self.data = extract_filing_data(_pdf("26047327"))

    def test_alphanumeric_address_parsed(self):
        assert "3351A" in self.data["registered_agent_address"]
        assert "Floyd" not in self.data["registered_agent_address"]

    def test_county(self):
        assert self.data["registered_agent_county"] == "Floyd"

    def test_organizer(self):
        assert self.data["organizer_name"] == "Sawyer Edens"
