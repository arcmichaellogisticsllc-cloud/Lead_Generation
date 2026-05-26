"""
Scoring calibration feedback — GA lead pipeline.

Analyzes conversion outcomes and optionally auto-tunes scoring_weights.yaml.

Sections:
  1. Conversion rates by tier / industry / priority
  2. Score accuracy by outcome (CONVERTED vs DEAD)
  3. Component-level analysis: which breakdown keys predict conversion
  4. Threshold analysis: where CONVERTED leads actually cluster
  5. Suggested weight adjustments (+ optional --apply)

Usage:
    python scripts/scoring_feedback.py                    # report only
    python scripts/scoring_feedback.py --apply            # apply component adjustments
    python scripts/scoring_feedback.py --apply --thresholds  # also shift hot/warm thresholds
    python scripts/scoring_feedback.py --days 60
    python scripts/scoring_feedback.py --history          # show past weight changes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

DB_PATH = PROJECT / "data" / "leads.db"

# Minimum converted + dead leads required before auto-apply fires
MIN_OUTCOMES_FOR_APPLY = 10


def get_conn(days: int) -> tuple[sqlite3.Connection, str]:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    date_filter = (
        f"AND date(last_updated) >= date('now', '-{days} days')" if days > 0 else ""
    )
    return conn, date_filter


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Section 1 — Conversion by segment
# ---------------------------------------------------------------------------

def report_conversion_by_segment(
    conn: sqlite3.Connection, date_filter: str
) -> list[dict]:
    _section("Conversion Rates by Tier / Industry / Priority")
    sql = f"""
        SELECT tier, industry_category, priority,
               COUNT(*) as leads,
               ROUND(AVG(fit_score), 1) as avg_score,
               SUM(outreach_status = 'CONVERTED') as converted,
               SUM(outreach_status = 'DEAD')      as dead,
               SUM(outreach_status = 'NO_CONTACT') as no_contact,
               SUM(outreach_status = 'NURTURE')   as nurture
        FROM leads
        WHERE outreach_status != 'NEW'
          {date_filter}
        GROUP BY tier, industry_category, priority
        HAVING leads >= 3
        ORDER BY converted DESC, leads DESC;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No outcome data yet (need outreach_status != 'NEW', ≥ 3 per group).")
        return []

    hdr = (
        f"  {'Tier':>4} {'Industry':<25} {'Priority':<8} "
        f"{'Leads':>6} {'AvgScore':>9} {'Conv':>5} {'Dead':>5} "
        f"{'NoCtct':>7} {'Nurture':>8}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    result = []
    for r in rows:
        industry = (r["industry_category"] or "unknown")[:25]
        conv_pct = f"{r['converted'] / r['leads']:.0%}" if r["leads"] else ""
        print(
            f"  {str(r['tier'] or '?'):>4} {industry:<25} {r['priority']:<8} "
            f"{r['leads']:>6} {r['avg_score']:>9} {r['converted']:>4}({conv_pct}) "
            f"{r['dead']:>5} {r['no_contact']:>7} {r['nurture']:>8}"
        )
        result.append(dict(r))
    return result


# ---------------------------------------------------------------------------
# Section 2 — Score accuracy by outcome
# ---------------------------------------------------------------------------

def report_score_accuracy(conn: sqlite3.Connection, date_filter: str) -> None:
    _section("Score Distribution by Outcome")
    sql = f"""
        SELECT outreach_status,
               COUNT(*) as n,
               ROUND(AVG(fit_score), 1)  as avg_score,
               MIN(fit_score)            as min_score,
               MAX(fit_score)            as max_score,
               SUM(filer_email IS NOT NULL OR business_email IS NOT NULL) as had_email,
               SUM(filer_phone IS NOT NULL OR business_phone IS NOT NULL) as had_phone,
               SUM(website IS NOT NULL)  as had_website
        FROM leads
        WHERE outreach_status IN ('CONVERTED','DEAD','NO_CONTACT','NURTURE')
          {date_filter}
        GROUP BY outreach_status;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No outcome data yet.")
        return

    hdr = (
        f"  {'Status':<12} {'N':>5} {'Avg':>6} {'Min':>5} {'Max':>5} "
        f"{'HadEmail':>9} {'HadPhone':>9} {'HadWeb':>7}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(
            f"  {r['outreach_status']:<12} {r['n']:>5} {r['avg_score']:>6} "
            f"{r['min_score']:>5} {r['max_score']:>5} "
            f"{r['had_email']:>9} {r['had_phone']:>9} {r['had_website']:>7}"
        )


# ---------------------------------------------------------------------------
# Section 3 — Component-level analysis
# ---------------------------------------------------------------------------

def report_component_accuracy(
    conn: sqlite3.Connection, date_filter: str
) -> dict[str, dict]:
    """Compare how often each score_breakdown component is nonzero in CONVERTED vs DEAD leads.

    Returns {component_key: {freq_conv, freq_dead, freq_diff, suggested_delta}}.
    """
    _section("Score Component Analysis (CONVERTED vs DEAD)")

    sql = f"""
        SELECT outreach_status, score_breakdown
        FROM leads
        WHERE outreach_status IN ('CONVERTED', 'DEAD')
          AND score_breakdown IS NOT NULL
          {date_filter};
    """
    rows = conn.execute(sql).fetchall()
    conv_bds: list[dict] = []
    dead_bds: list[dict] = []
    for r in rows:
        bd = {}
        try:
            bd = json.loads(r["score_breakdown"])
        except Exception:
            pass
        if r["outreach_status"] == "CONVERTED":
            conv_bds.append(bd)
        else:
            dead_bds.append(bd)

    n_conv = len(conv_bds)
    n_dead = len(dead_bds)

    if n_conv < 3 or n_dead < 3:
        print(f"  Not enough outcome data (need ≥ 3 CONVERTED + ≥ 3 DEAD; "
              f"have {n_conv} converted, {n_dead} dead).")
        return {}

    print(f"  Analyzing {n_conv} converted, {n_dead} dead leads.\n")

    # Collect all component keys
    all_keys: set[str] = set()
    for bd in conv_bds + dead_bds:
        all_keys.update(bd.keys())

    stats: dict[str, dict] = {}
    rows_out: list[tuple] = []

    for key in sorted(all_keys):
        freq_conv = sum(1 for bd in conv_bds if bd.get(key, 0) != 0) / n_conv
        freq_dead = sum(1 for bd in dead_bds if bd.get(key, 0) != 0) / n_dead
        diff      = freq_conv - freq_dead
        # Scale diff to a weight-point suggestion: 5 pts per 100% frequency gap, clamped ±2
        suggested = max(-2, min(2, round(diff * 5)))
        stats[key] = {
            "freq_conv":     freq_conv,
            "freq_dead":     freq_dead,
            "freq_diff":     diff,
            "suggested_delta": suggested,
        }
        rows_out.append((abs(diff), key, freq_conv, freq_dead, diff, suggested))

    rows_out.sort(reverse=True)
    from src.weight_manager import BREAKDOWN_TO_YAML

    print(
        f"  {'Component':<35} {'Conv%':>6} {'Dead%':>6} "
        f"{'Δfreq':>7} {'Adj':>4}  {'YAML key'}"
    )
    print("  " + "-" * 72)
    for _, key, fc, fd, diff, sug in rows_out:
        yaml_path = BREAKDOWN_TO_YAML.get(key)
        yaml_label = f"{yaml_path[0]}.{yaml_path[1]}" if yaml_path else "(manual)"
        arrow = "↑" if sug > 0 else ("↓" if sug < 0 else " ")
        print(
            f"  {key:<35} {fc:>5.0%} {fd:>6.0%} "
            f"{diff:>+7.0%} {arrow}{abs(sug):>3}  {yaml_label}"
        )

    return stats


# ---------------------------------------------------------------------------
# Section 4 — Threshold analysis
# ---------------------------------------------------------------------------

def report_threshold_analysis(
    conn: sqlite3.Connection, date_filter: str
) -> dict[str, int]:
    """Suggest hot/warm threshold shifts based on where CONVERTED leads score."""
    _section("Priority Threshold Analysis")

    from src.weight_manager import load_weights
    weights   = load_weights()
    thresholds = weights.get("priority_tiers", {})
    hot_t  = thresholds.get("hot_threshold",  80)
    warm_t = thresholds.get("warm_threshold", 60)
    cold_t = thresholds.get("cold_threshold", 40)

    print(f"  Current thresholds — HOT ≥ {hot_t}  WARM ≥ {warm_t}  COLD ≥ {cold_t}")

    sql = f"""
        SELECT fit_score, outreach_status
        FROM leads
        WHERE outreach_status IN ('CONVERTED', 'DEAD')
          AND fit_score IS NOT NULL
          {date_filter}
        ORDER BY fit_score;
    """
    rows = conn.execute(sql).fetchall()
    conv_scores = [r["fit_score"] for r in rows if r["outreach_status"] == "CONVERTED"]
    dead_scores = [r["fit_score"] for r in rows if r["outreach_status"] == "DEAD"]

    if len(conv_scores) < 3:
        print("  Not enough CONVERTED leads for threshold analysis.")
        return {}

    def pct(scores: list[int], p: float) -> float:
        if not scores:
            return 0.0
        idx = int(len(scores) * p)
        return scores[min(idx, len(scores) - 1)]

    conv_scores.sort()
    dead_scores.sort()

    p25_conv = pct(conv_scores, 0.25)
    p50_conv = pct(conv_scores, 0.50)
    p75_conv = pct(conv_scores, 0.75)
    p50_dead = pct(dead_scores, 0.50) if dead_scores else None

    print(
        f"\n  CONVERTED score distribution (n={len(conv_scores)}): "
        f"p25={p25_conv:.0f}  p50={p50_conv:.0f}  p75={p75_conv:.0f}"
    )
    if dead_scores:
        print(
            f"  DEAD score distribution    (n={len(dead_scores)}): "
            f"p50={p50_dead:.0f}"
        )

    deltas: dict[str, int] = {}

    # If the median CONVERTED lead scores below hot_threshold, we're mis-classifying them
    if p50_conv < hot_t - 5:
        shift = max(-5, round(p50_conv - hot_t + 3))
        print(f"\n  → Suggest lowering hot_threshold by {abs(shift)} "
              f"(median converted score {p50_conv:.0f} < threshold {hot_t})")
        deltas["hot_threshold"]  = shift

    # If the 25th percentile of CONVERTED leads is below warm_threshold
    if p25_conv < warm_t - 5:
        shift = max(-5, round(p25_conv - warm_t + 3))
        print(f"  → Suggest lowering warm_threshold by {abs(shift)} "
              f"(p25 converted score {p25_conv:.0f} < threshold {warm_t})")
        deltas["warm_threshold"] = shift

    if not deltas:
        print(
            f"\n  Thresholds look well-calibrated "
            f"(median converted score {p50_conv:.0f} is above hot_threshold {hot_t})."
        )

    return deltas


# ---------------------------------------------------------------------------
# Section 5 — Industry weight suggestions
# ---------------------------------------------------------------------------

def report_suggested_adjustments(
    segment_rows: list[dict],
    conn: sqlite3.Connection,
    date_filter: str,
) -> None:
    _section("Industry-Level Adjustment Suggestions")

    if not segment_rows:
        print("  No segment data available.")
        return

    sql = f"""
        SELECT COUNT(*) as total,
               SUM(outreach_status = 'CONVERTED') as converted
        FROM leads WHERE outreach_status != 'NEW' {date_filter};
    """
    overall = conn.execute(sql).fetchone()
    total, conv = (overall["total"] or 0), (overall["converted"] or 0)
    if total == 0:
        print("  No data.")
        return
    overall_rate = conv / total
    print(f"  Overall conversion rate: {conv}/{total} = {overall_rate:.1%}")
    print()

    industry_stats: dict[str, dict] = {}
    for r in segment_rows:
        cat = r.get("industry_category") or "unknown"
        s   = industry_stats.setdefault(cat, {"leads": 0, "converted": 0})
        s["leads"]     += r.get("leads", 0) or 0
        s["converted"] += r.get("converted", 0) or 0

    found = False
    for cat, s in sorted(industry_stats.items()):
        if s["leads"] == 0:
            continue
        rate  = s["converted"] / s["leads"]
        ratio = rate / overall_rate if overall_rate > 0 else 0.0
        if ratio > 2.0:
            print(f"  ↑ INCREASE tier weight for '{cat}': "
                  f"{rate:.1%} conv ({ratio:.1f}x avg)")
            found = True
        elif ratio < 0.5 and s["leads"] >= 3:
            print(f"  ↓ DECREASE tier weight for '{cat}': "
                  f"{rate:.1%} conv ({ratio:.1f}x avg)")
            found = True
    if not found:
        print("  No anomalies — all industries within 0.5×–2× of overall rate.")


# ---------------------------------------------------------------------------
# Weight history display
# ---------------------------------------------------------------------------

def show_history() -> None:
    _section("Weight Change History (last 10)")
    from src.weight_manager import weight_history
    entries = weight_history(10)
    if not entries:
        print("  No weight changes recorded yet.")
        return
    for entry in entries:
        print(f"\n  [{entry['timestamp'][:19]}]  {entry['reason']}")
        for key, chg in entry.get("changes", {}).items():
            arrow = "↑" if chg["delta"] > 0 else "↓"
            print(f"    {arrow} {key}: {chg['old']} → {chg['new']}  ({chg['delta']:+d})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GA lead pipeline scoring calibration report"
    )
    parser.add_argument("--days",        type=int,  default=90,
                        help="Lookback window (default 90, 0 = all time)")
    parser.add_argument("--apply",       action="store_true",
                        help="Auto-apply component weight adjustments to scoring_weights.yaml")
    parser.add_argument("--thresholds",  action="store_true",
                        help="Also apply hot/warm threshold shifts (use with --apply)")
    parser.add_argument("--history",     action="store_true",
                        help="Show weight change history and exit")
    parser.add_argument("--min-outcomes", type=int, default=MIN_OUTCOMES_FOR_APPLY,
                        dest="min_outcomes",
                        help=f"Min CONVERTED+DEAD leads for --apply (default {MIN_OUTCOMES_FOR_APPLY})")
    args = parser.parse_args()

    if args.history:
        show_history()
        return

    conn, date_filter = get_conn(args.days)
    lookback = f"last {args.days} days" if args.days > 0 else "all time"
    print(f"\nGA Lead Pipeline — Scoring Feedback ({lookback})")

    segment_rows = report_conversion_by_segment(conn, date_filter)
    report_score_accuracy(conn, date_filter)
    component_stats = report_component_accuracy(conn, date_filter)
    threshold_deltas = report_threshold_analysis(conn, date_filter)
    report_suggested_adjustments(segment_rows, conn, date_filter)

    if args.apply:
        _section("Applying Weight Adjustments")
        from src.weight_manager import apply_adjustments, apply_threshold_adjustments

        # Check we have enough data
        n_total = sum(
            1 for r in (conn.execute(
                f"SELECT outreach_status FROM leads "
                f"WHERE outreach_status IN ('CONVERTED','DEAD') {date_filter}"
            ).fetchall())
        )
        if n_total < args.min_outcomes:
            print(
                f"  SKIPPED — only {n_total} CONVERTED/DEAD outcomes; "
                f"need ≥ {args.min_outcomes} for auto-apply.\n"
                f"  (Lower --min-outcomes to override.)"
            )
        else:
            # Build adjustments dict from component analysis
            adj = {
                k: v["suggested_delta"]
                for k, v in component_stats.items()
                if v["suggested_delta"] != 0
            }
            if adj:
                applied = apply_adjustments(adj, reason=f"scoring_feedback --days {args.days}")
                if applied:
                    print("  Component weights updated:")
                    for key, chg in applied.items():
                        arrow = "↑" if chg["delta"] > 0 else "↓"
                        print(f"    {arrow} {key}: {chg['old']} → {chg['new']}")
                else:
                    print("  No component changes applied (all deltas rounded to 0).")
            else:
                print("  No component adjustments to apply.")

            if args.thresholds and threshold_deltas:
                t_applied = apply_threshold_adjustments(
                    hot_delta  = threshold_deltas.get("hot_threshold",  0),
                    warm_delta = threshold_deltas.get("warm_threshold", 0),
                    cold_delta = threshold_deltas.get("cold_threshold", 0),
                    reason     = f"scoring_feedback --thresholds --days {args.days}",
                )
                if t_applied:
                    print("  Threshold changes applied:")
                    for key, chg in t_applied.items():
                        arrow = "↑" if chg["delta"] > 0 else "↓"
                        print(f"    {arrow} {key}: {chg['old']} → {chg['new']}")
    else:
        if component_stats or threshold_deltas:
            print(
                "\n  Run with --apply to write these adjustments to "
                "config/scoring_weights.yaml."
            )

    conn.close()
    print()


if __name__ == "__main__":
    main()
