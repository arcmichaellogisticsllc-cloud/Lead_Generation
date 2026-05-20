"""
Parse eCorp filing PDFs (Articles of Organization / Business Formation) for
contact and organizer data.

PDF structure (observed from Georgia eCorp 2026 filings):
  Page 1: Certificate of Organization (government seal page — skip)
  Page 2: Articles of Organization — structured all-caps label/value layout:
    CONTROL NUMBER  <8-digit>
    BUSINESS NAME   <name>
    BUSINESS TYPE   <type>
    EFFECTIVE DATE  <MM/DD/YYYY>
    PRINCIPAL OFFICE ADDRESS
    ADDRESS         <street, city, state, zip>
    REGISTERED AGENT
    NAME  ADDRESS  COUNTY
    <name>  <addr>  <county>
    ORGANIZER(S)
    NAME  TITLE  ADDRESS
    <name>  ORGANIZER  <addr>
    AUTHORIZER INFORMATION
    AUTHORIZER SIGNATURE  <name>
    AUTHORIZER TITLE      <title>

Note: GA online filings do NOT include email or phone in the downloadable PDF.
Those fields will be None.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_filing_data(pdf_path: Path | str, doc_type: str = "formation") -> dict:
    """Extract contact and entity fields from a GA Articles of Organization PDF.

    Returns:
        {
          control_number, entity_name, entity_type, effective_date,
          principal_office_address,
          registered_agent_name, registered_agent_address, registered_agent_county,
          organizers: [{name, title, address}],
          organizer_name,      # first organizer's name (convenience field)
          filer_email,         # always None (not in GA PDF)
          filer_phone,         # always None (not in GA PDF)
          authorizer_name, authorizer_title,
          pages_parsed,
        }
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber is required: pip install pdfplumber")

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    result: dict[str, Any] = {
        "control_number":             None,
        "entity_name":                None,
        "entity_type":                None,
        "effective_date":             None,
        "principal_office_address":   None,
        "registered_agent_name":      None,
        "registered_agent_address":   None,
        "registered_agent_county":    None,
        "organizers":                 [],
        "organizer_name":             None,
        "filer_email":                None,  # not available in GA PDF
        "filer_phone":                None,  # not available in GA PDF
        "authorizer_name":            None,
        "authorizer_title":           None,
        "pages_parsed":               0,
    }

    with pdfplumber.open(str(pdf_path)) as pdf:
        result["pages_parsed"] = len(pdf.pages)
        pages_text = [pg.extract_text() or "" for pg in pdf.pages]

    # Page 1 is the certificate (seal page) — contains minimal data
    # Page 2+ is the Articles — contains structured data
    articles_text = pages_text[1] if len(pages_text) > 1 else pages_text[0]

    result.update(_parse_articles_page(articles_text))

    # Convenience field: first organizer name
    if result["organizers"]:
        result["organizer_name"] = result["organizers"][0].get("name")

    logger.debug(
        "Extracted %s: organizer=%s addr=%s",
        pdf_path.name,
        result.get("organizer_name"),
        result.get("principal_office_address", "")[:40],
    )
    return result


# ---------------------------------------------------------------------------
# Articles page parser
# ---------------------------------------------------------------------------

def _parse_articles_page(text: str) -> dict:
    """Parse the Articles of Organization page text using regex label matching."""
    data: dict[str, Any] = {}

    # --- Single-value fields ---
    data["control_number"]  = _field(text, r"CONTROL\s+NUMBER\s+(\d{8})")
    data["entity_name"]     = _field(text, r"BUSINESS\s+NAME\s+(.+?)(?=\s+BUSINESS\s+TYPE|\s+EFFECTIVE|\Z)")
    data["entity_type"]     = _field(text, r"BUSINESS\s+TYPE\s+(.+?)(?=\s+EFFECTIVE|\Z)")
    data["effective_date"]  = _field(text, r"EFFECTIVE\s+DATE\s+(\d{1,2}/\d{1,2}/\d{4})")

    # Principal office address — appears after "ADDRESS" label in the PRINCIPAL block
    data["principal_office_address"] = _field(
        text,
        r"PRINCIPAL\s+OFFICE\s+ADDRESS\s+ADDRESS\s+(.+?)(?=\s+REGISTERED|\Z)",
    )

    # --- Registered agent block ---
    ra = _parse_registered_agent(text)
    data.update(ra)

    # --- Organizers table ---
    data["organizers"] = _parse_organizers(text)

    # --- Authorizer ---
    data["authorizer_name"]  = _field(text, r"AUTHORIZER\s+SIGNATURE\s+(.+?)(?=\s+AUTHORIZER\s+TITLE|\Z)")
    data["authorizer_title"] = _field(text, r"AUTHORIZER\s+TITLE\s+(.+?)(?=\n|\Z)")

    return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}


def _parse_registered_agent(text: str) -> dict:
    """Extract name, address, county from the REGISTERED AGENT section.

    Two PDF layouts observed:
      Standard: one line — "Name 123 Street, City, GA, zip, USA County"
      Wrapped:  address column renders before name line in pdfplumber's output,
                producing multi-line blocks like:
                  "1796 SATELLITE BLVD...,\\nkyung hee yoon Gwinnett\\nUSA"
    """
    m = re.search(
        r"REGISTERED\s+AGENT\s+NAME\s+ADDRESS\s+COUNTY\s+(.+?)(?=\n\s*ORGANIZER|\n\s*OPTIONAL|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return {}

    block = m.group(1).strip()
    lines = [l.strip() for l in block.split("\n") if l.strip()]

    # Wrapped format: first line starts with a digit (address column comes first)
    if lines and re.match(r"^\d", lines[0]):
        return _parse_ra_wrapped(lines)
    return _parse_ra_standard(" ".join(lines))


def _parse_ra_standard(single_line: str) -> dict:
    """Parse 'Name <digit-addr> ... USA County' on one line."""
    m = re.search(
        r"^(.+?)\s+(\d+\S*\s+.+?\bUSA\b)\s+(\w+)\s*$",
        single_line,
        re.IGNORECASE,
    )
    if m:
        return {
            "registered_agent_name":    m.group(1).strip(),
            "registered_agent_address": m.group(2).strip(),
            "registered_agent_county":  m.group(3).strip(),
        }
    words = single_line.split()
    return {
        "registered_agent_name":    " ".join(words[:2]) if len(words) >= 2 else single_line,
        "registered_agent_address": " ".join(words[2:-1]) if len(words) > 3 else None,
        "registered_agent_county":  words[-1] if len(words) > 2 else None,
    }


def _parse_ra_wrapped(lines: list) -> dict:
    """Parse wrapped-column RA block where address lines precede the name line."""
    address_parts: list[str] = []
    agent_name   = None
    agent_county = None

    for line in lines:
        if re.search(r",\s*GA\b|,\s*\d{5}", line, re.IGNORECASE) or line.upper() == "USA":
            address_parts.append(line)
        else:
            words = line.split()
            agent_county = words[-1] if len(words) >= 2 else None
            agent_name   = " ".join(words[:-1]) if len(words) >= 2 else line

    addr = " ".join(address_parts)
    addr = re.sub(r",?\s*$", "", addr).strip()
    if addr and not addr.upper().endswith("USA"):
        addr += " USA"

    return {
        "registered_agent_name":    agent_name,
        "registered_agent_address": addr or None,
        "registered_agent_county":  agent_county,
    }


def _parse_organizers(text: str) -> list[dict]:
    """Extract the ORGANIZER(S) table rows."""
    # Block:
    # ORGANIZER(S)
    # NAME  TITLE  ADDRESS
    # Dailton Diaz  ORGANIZER  4485 Lawrenceville Hwy, Lilburn, GA, 30047, USA
    m = re.search(
        r"ORGANIZER\(S\)\s+NAME\s+TITLE\s+ADDRESS\s+(.+?)(?=\s+OPTIONAL|\s+AUTHORIZER|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []

    block = m.group(1).strip()
    organizers: list[dict] = []

    # Each row: <name> <TITLE_KEYWORD> <address>
    title_keywords = r"(?:ORGANIZER|MEMBER|MANAGER|REGISTERED\s+AGENT)"
    for row_m in re.finditer(
        rf"(.+?)\s+({title_keywords})\s+(.+?)(?=\n|$)",
        block,
        re.IGNORECASE,
    ):
        name  = row_m.group(1).strip()
        title = row_m.group(2).strip()
        addr  = row_m.group(3).strip()
        if name:
            organizers.append({"name": name, "title": title, "address": addr})

    return organizers


def _field(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from rich.console import Console
    from rich import print as rprint

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Checkpoint 4: extract data from eCorp filing PDFs"
    )
    parser.add_argument(
        "--pdfs",
        nargs="*",
        help="Specific PDF paths to parse (default: all in data/raw/)",
    )
    args = parser.parse_args()

    console = Console()

    raw_dir = Path(__file__).parent.parent / "data" / "raw"
    if args.pdfs:
        pdf_paths = [Path(p) for p in args.pdfs]
    else:
        pdf_paths = sorted(raw_dir.glob("*_formation.pdf"))

    if not pdf_paths:
        console.print("[yellow]No PDFs found. Run download_pdfs.py first.[/yellow]")
        sys.exit(0)

    console.print(f"\n[bold cyan]Extracting {len(pdf_paths)} PDF(s)[/bold cyan]\n")

    for pdf_path in pdf_paths:
        console.print(f"[bold]{pdf_path.name}[/bold]")
        try:
            data = extract_filing_data(pdf_path)
            for k, v in data.items():
                if k == "organizers":
                    for i, org in enumerate(v):
                        console.print(f"  [dim]organizer[{i}]:[/dim] {org}")
                else:
                    console.print(f"  [dim]{k}:[/dim] {v}")
        except Exception as exc:
            console.print(f"  [red]Error: {exc}[/red]")
        console.print()
