"""
Classify entities by NAICS code and name keywords.
Implemented at Checkpoint 2.
"""
from __future__ import annotations
from pathlib import Path
import yaml

_CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((_CONFIG_DIR / name).read_text())


def _load_registered_agents() -> set[str]:
    path = _CONFIG_DIR / "registered_agent_services.txt"
    return {line.strip().lower() for line in path.read_text().splitlines() if line.strip()}


# Lazy-loaded config
_naics_map: dict[str, tuple[int, str]] | None = None
_keywords: dict[int, list[str]] | None = None
_disqualifiers: list[str] | None = None
_registered_agents: set[str] | None = None


def _get_naics_map() -> dict[str, tuple[int, str]]:
    global _naics_map
    if _naics_map is None:
        cfg = _load_yaml("target_naics.yaml")
        _naics_map = {}
        for tier_key, tier_data in cfg.items():
            tier_num = int(tier_key.split("_")[1])
            for industry in tier_data["industries"]:
                _naics_map[industry["naics"]] = (tier_num, industry["name"])
    return _naics_map


def _get_keywords() -> dict[int, list[str]]:
    global _keywords
    if _keywords is None:
        cfg = _load_yaml("keywords.yaml")
        _keywords = {
            1: [kw.lower() for kw in cfg.get("tier_1", [])],
            2: [kw.lower() for kw in cfg.get("tier_2", [])],
            3: [kw.lower() for kw in cfg.get("tier_3", [])],
        }
    return _keywords


def _get_disqualifiers() -> list[str]:
    global _disqualifiers
    if _disqualifiers is None:
        cfg = _load_yaml("disqualifier_keywords.yaml")
        _disqualifiers = [d.lower() for d in cfg.get("disqualifiers", [])]
    return _disqualifiers


def _get_registered_agents() -> set[str]:
    global _registered_agents
    if _registered_agents is None:
        _registered_agents = _load_registered_agents()
    return _registered_agents


def classify_entity(entity_data: dict) -> dict:
    """Return {tier, industry_category, match_source} or tier=None if disqualified/unmatched."""
    name_lower = (entity_data.get("entity_name") or "").lower()

    # Disqualifier check first
    for disq in _get_disqualifiers():
        if disq in name_lower:
            return {"tier": None, "industry_category": None, "match_source": "disqualified"}

    naics = (entity_data.get("naics_code") or "").strip()
    naics_map = _get_naics_map()
    keywords = _get_keywords()

    naics_match: tuple[int, str] | None = None
    keyword_match: tuple[int, str] | None = None

    if naics and naics in naics_map:
        naics_match = naics_map[naics]

    for tier_num in (1, 2, 3):
        for kw in keywords[tier_num]:
            if kw in name_lower:
                keyword_match = (tier_num, kw)
                break
        if keyword_match:
            break

    if naics_match and keyword_match:
        tier, category = naics_match
        return {"tier": tier, "industry_category": category, "match_source": "both"}
    elif naics_match:
        tier, category = naics_match
        return {"tier": tier, "industry_category": category, "match_source": "naics"}
    elif keyword_match:
        tier, kw = keyword_match
        return {"tier": tier, "industry_category": kw, "match_source": "keyword"}

    return {"tier": None, "industry_category": None, "match_source": "no_match"}


def is_registered_agent_service(agent_name: str) -> bool:
    """Return True if the registered agent name matches a known RA service."""
    name_lower = (agent_name or "").lower()
    for svc in _get_registered_agents():
        if svc in name_lower:
            return True
    return False


if __name__ == "__main__":
    samples = [
        {"entity_name": "Peach State Plumbing LLC", "naics_code": ""},
        {"entity_name": "Atlanta HVAC Solutions LLC", "naics_code": "238220"},
        {"entity_name": "Buckhead Real Estate Holdings LLC", "naics_code": ""},
        {"entity_name": "Quick Cuts Barber Shop LLC", "naics_code": ""},
        {"entity_name": "Sunrise Automotive Repair LLC", "naics_code": "811111"},
        {"entity_name": "Generic Business Services Inc", "naics_code": ""},
    ]
    print(f"{'Entity Name':<45} {'Tier':<6} {'Match Source':<14} {'Category'}")
    print("-" * 90)
    for s in samples:
        result = classify_entity(s)
        print(
            f"{s['entity_name']:<45} {str(result['tier']):<6} "
            f"{result['match_source']:<14} {result['industry_category']}"
        )
