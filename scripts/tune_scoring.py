"""
Interactive scoring tuner.

Reviews the top 50 scored leads, asks "Good lead? (y/n/skip)" for each,
then suggests scoring weight adjustments based on correlation between
manual labels and score components.

Does NOT auto-update weights — only suggests changes.

Usage:
  python scripts/tune_scoring.py
  python scripts/tune_scoring.py --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import DB_PATH, get_connection, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive scoring tuner")
    parser.add_argument("--limit", type=int, default=50, help="Leads to review")
    parser.add_argument(
        "--priority", default="ALL",
        choices=["HOT", "WARM", "COLD", "ALL"],
        help="Filter by priority (default: ALL)",
    )
    args = parser.parse_args()

    init_db()
    conn = get_connection(DB_PATH)

    if args.priority == "ALL":
        rows = conn.execute(
            "SELECT * FROM leads WHERE priority != 'SKIP' ORDER BY fit_score DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads WHERE priority=? ORDER BY fit_score DESC LIMIT ?",
            (args.priority, args.limit),
        ).fetchall()
    conn.close()

    if not rows:
        print(f"No leads found (priority={args.priority}).")
        sys.exit(0)

    print(f"\nReviewing {len(rows)} leads. Press Ctrl-C to stop early.\n")
    print("For each lead, enter:  y = good lead   n = bad lead   s = skip\n")
    print("─" * 70)

    labels:     dict[str, str]   = {}   # control_number → "y"|"n"
    breakdowns: dict[str, dict]  = {}

    for i, row in enumerate(rows, 1):
        cn   = row["control_number"]
        bd   = json.loads(row["score_breakdown"] or "{}")
        breakdowns[cn] = bd

        print(f"\n[{i}/{len(rows)}]  {row['entity_name']}")
        print(f"  Score: {row['fit_score']}   Priority: {row['priority']}")
        print(f"  Category: {row['industry_category'] or '—'}   Tier: {row['tier']}")
        print(f"  Organizer: {row['organizer_name'] or '—'}")
        print(f"  Address:   {row['principal_office_address'] or '—'}")
        print(f"  Website:   {row['website'] or '—'}")
        print(f"  Processor: {row['detected_payment_processor'] or '—'}")
        print(f"  Breakdown: {_fmt_breakdown(bd)}")

        while True:
            try:
                answer = input("  Good lead? [y/n/s]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n[stopped]")
                _analyze_and_suggest(labels, breakdowns)
                return
            if answer in ("y", "n", "s", "skip"):
                if answer in ("y", "n"):
                    labels[cn] = answer
                break
            print("  Enter y, n, or s")

    print("\n" + "─" * 70)
    _analyze_and_suggest(labels, breakdowns)


def _analyze_and_suggest(
    labels: dict[str, str],
    breakdowns: dict[str, dict],
) -> None:
    if len(labels) < 3:
        print("\nNot enough labeled leads to suggest adjustments (need ≥ 3).")
        return

    good_cn = {cn for cn, lbl in labels.items() if lbl == "y"}
    bad_cn  = {cn for cn, lbl in labels.items() if lbl == "n"}

    print(f"\nLabeled {len(labels)} leads: {len(good_cn)} good, {len(bad_cn)} bad\n")

    # Collect all component keys across all breakdowns
    all_keys: set[str] = set()
    for bd in breakdowns.values():
        all_keys.update(bd.keys())

    suggestions: list[tuple[float, str, str]] = []

    for key in sorted(all_keys):
        good_vals = [breakdowns[cn].get(key, 0) for cn in good_cn if cn in breakdowns]
        bad_vals  = [breakdowns[cn].get(key, 0) for cn in bad_cn  if cn in breakdowns]
        if not good_vals and not bad_vals:
            continue
        avg_good = sum(good_vals) / len(good_vals) if good_vals else 0
        avg_bad  = sum(bad_vals)  / len(bad_vals)  if bad_vals  else 0
        diff     = avg_good - avg_bad
        suggestions.append((diff, key, f"avg_good={avg_good:.1f}  avg_bad={avg_bad:.1f}"))

    suggestions.sort(key=lambda x: abs(x[0]), reverse=True)

    print("Score component correlations with 'good lead' labels:")
    print(f"{'Component':<35} {'Δ (good−bad)':>12}  Notes")
    print("─" * 70)
    for diff, key, note in suggestions[:15]:
        direction = "↑ raise weight" if diff > 0 else "↓ lower weight"
        print(f"  {key:<33} {diff:>+8.1f}   {direction}  ({note})")

    print(
        "\n[tune_scoring] These are suggestions only. "
        "Edit config/scoring_weights.yaml manually, then run:\n"
        "  python src/pipeline.py score\n"
        "  python src/pipeline.py export\n"
        "to see updated rankings."
    )


def _fmt_breakdown(bd: dict) -> str:
    parts = [f"{k}={v}" for k, v in bd.items() if v]
    return "  ".join(parts) if parts else "—"


if __name__ == "__main__":
    main()
