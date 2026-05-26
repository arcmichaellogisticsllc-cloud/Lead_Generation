"""
Read, adjust, and audit changes to config/scoring_weights.yaml.

Provides a single apply_adjustments() entry point used by both the feedback
script (data-driven, runs automatically) and the interactive tuner (manual).
Every change is backed up and appended to data/weight_history.jsonl.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_DIR   = Path(__file__).parent.parent / "config"
WEIGHTS_PATH = CONFIG_DIR / "scoring_weights.yaml"
HISTORY_PATH = Path(__file__).parent.parent / "data" / "weight_history.jsonl"

# Hard cap per key per auto-apply run — prevents calibration from running away
MAX_DELTA = 2

# Map score_breakdown keys → (yaml_section, yaml_key)
# Keys not listed here are either in payment_stack_fingerprints.yaml or
# computed dynamically (industry) and require manual editing.
BREAKDOWN_TO_YAML: dict[str, tuple[str, str]] = {
    "residential_registered_agent": ("owner_operator_signals", "residential_registered_agent"),
    "individual_organizer":         ("owner_operator_signals", "individual_organizer"),
    "llc_not_inc":                  ("owner_operator_signals", "llc_not_inc"),
    "within_14_days":               ("recency", "within_14_days"),
    "within_7_days":                ("recency", "within_7_days"),
    "has_filer_email":              ("contactability", "has_filer_email"),
    "has_filer_phone":              ("contactability", "has_filer_phone"),
    "has_organizer_name":           ("contactability", "has_organizer_name"),
    "no_contact_at_all":            ("contactability", "no_contact_at_all"),
}

# Keys that are intentionally negative — don't clamp their new value to ≥ 0
NEGATIVE_OK = {"no_contact_at_all"}


def load_weights() -> dict:
    return yaml.safe_load(WEIGHTS_PATH.read_text())


def apply_adjustments(
    adjustments: dict[str, int | float],
    reason: str = "auto-calibration",
    max_delta: int = MAX_DELTA,
) -> dict[str, dict]:
    """Apply {breakdown_key: suggested_delta} to scoring_weights.yaml.

    Returns the actual changes written: {section.key: {"old": x, "new": y, "delta": d}}.
    Skips unknown keys and clamps every delta to ±max_delta.
    Writes a timestamped backup and appends to data/weight_history.jsonl.
    """
    weights = load_weights()
    applied: dict[str, dict] = {}

    for bkey, raw_delta in adjustments.items():
        mapping = BREAKDOWN_TO_YAML.get(bkey)
        if mapping is None:
            continue
        section, key = mapping
        if section not in weights or key not in weights[section]:
            continue

        clamped = max(-max_delta, min(max_delta, round(raw_delta)))
        if clamped == 0:
            continue

        old_val = weights[section][key]
        new_val = old_val + clamped
        if key not in NEGATIVE_OK:
            new_val = max(0, new_val)

        weights[section][key] = new_val
        applied[f"{section}.{key}"] = {"old": old_val, "new": new_val, "delta": clamped}

    if not applied:
        return {}

    _backup(WEIGHTS_PATH)
    WEIGHTS_PATH.write_text(
        yaml.dump(weights, default_flow_style=False, sort_keys=False)
    )

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "reason":    reason,
        "changes":   applied,
    }
    with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    return applied


def apply_threshold_adjustments(
    hot_delta: int = 0,
    warm_delta: int = 0,
    cold_delta: int = 0,
    reason: str = "threshold-calibration",
) -> dict[str, dict]:
    """Adjust hot/warm/cold thresholds by the given signed deltas."""
    adjustments_raw: dict[str, int] = {}
    if hot_delta:
        adjustments_raw["_hot_threshold"]  = hot_delta
    if warm_delta:
        adjustments_raw["_warm_threshold"] = warm_delta
    if cold_delta:
        adjustments_raw["_cold_threshold"] = cold_delta

    if not adjustments_raw:
        return {}

    weights = load_weights()
    applied: dict[str, dict] = {}
    tiers = weights.setdefault("priority_tiers", {})

    for key, delta in adjustments_raw.items():
        yaml_key = key.lstrip("_")
        if yaml_key not in tiers:
            continue
        clamped = max(-3, min(3, delta))
        if clamped == 0:
            continue
        old_val = tiers[yaml_key]
        tiers[yaml_key] = old_val + clamped
        applied[f"priority_tiers.{yaml_key}"] = {
            "old": old_val, "new": tiers[yaml_key], "delta": clamped
        }

    if not applied:
        return {}

    _backup(WEIGHTS_PATH)
    WEIGHTS_PATH.write_text(
        yaml.dump(weights, default_flow_style=False, sort_keys=False)
    )
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now().isoformat(), "reason": reason, "changes": applied}
    with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    return applied


def weight_history(last_n: int = 10) -> list[dict]:
    """Return the last N weight-change events, newest first."""
    if not HISTORY_PATH.exists():
        return []
    lines = HISTORY_PATH.read_text().strip().splitlines()
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return list(reversed(entries[-last_n:]))


def _backup(path: Path) -> Path:
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path.with_suffix(f".{ts}.bak.yaml")
    shutil.copy2(path, bak)
    return bak
