"""
Scoring calibration feedback script for the GA lead generation pipeline.

Queries conversion outcomes from the DB and generates a calibration report:
  - Conversion rates by tier / industry / priority
  - Score breakdown accuracy (converted vs. dead leads)
  - Suggested weight adjustments for industries with anomalous conversion rates

Usage:
    python3 scripts/scoring_feedback.py
    python3 scripts/scoring_feedback.py --days 60
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
DB_PATH = PROJECT / "data" / "leads.db"


def get_conn(days: int) -> tuple[sqlite3.Connection, str]:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # Build a date filter expression
    date_filter = f"AND date(last_updated) >= date('now', '-{days} days')" if days > 0 else ""
    return conn, date_filter


def print_section(title: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def report_conversion_by_segment(conn: sqlite3.Connection, date_filter: str) -> list[dict]:
    print_section("Conversion Rates by Tier / Industry / Priority")
    sql = f"""
        SELECT tier, industry_category, priority,
               COUNT(*) as leads,
               ROUND(AVG(fit_score), 1) as avg_score,
               SUM(outreach_status = 'CONVERTED') as converted,
               SUM(outreach_status = 'DEAD') as dead,
               SUM(outreach_status = 'NO_CONTACT') as no_contact,
               SUM(outreach_status = 'NURTURE') as nurture
        FROM leads
        WHERE outreach_status != 'NEW'
          {date_filter}
        GROUP BY tier, industry_category, priority
        HAVING leads >= 3
        ORDER BY converted DESC, leads DESC;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No outcome data found (need outreach_status != 'NEW' with >= 3 leads per group).")
        return []

    header = (
        f"  {'Tier':>4} {'Industry':<25} {'Priority':<8} "
        f"{'Leads':>6} {'AvgScore':>9} {'Conv':>5} {'Dead':>5} "
        f"{'NoCtct':>7} {'Nurture':>8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    result = []
    for r in rows:
        industry = (r["industry_category"] or "unknown")[:25]
        print(
            f"  {str(r['tier'] or '?'):>4} {industry:<25} {r['priority']:<8} "
            f"{r['leads']:>6} {r['avg_score']:>9} {r['converted']:>5} {r['dead']:>5} "
            f"{r['no_contact']:>7} {r['nurture']:>8}"
        )
        result.append(dict(r))
    return result


def report_score_accuracy(conn: sqlite3.Connection, date_filter: str) -> None:
    print_section("Score Breakdown Accuracy (by Outcome)")
    sql = f"""
        SELECT outreach_status,
               COUNT(*) as n,
               ROUND(AVG(fit_score), 1) as avg_score,
               SUM(filer_email IS NOT NULL) as had_email,
               SUM(filer_phone IS NOT NULL) as had_phone,
               SUM(website IS NOT NULL) as had_website
        FROM leads
        WHERE outreach_status IN ('CONVERTED','DEAD','NO_CONTACT','NURTURE')
          {date_filter}
        GROUP BY outreach_status;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No outcome data found.")
        return

    header = (
        f"  {'Status':<12} {'N':>5} {'AvgScore':>9} "
        f"{'HadEmail':>9} {'HadPhone':>9} {'HadWeb':>7}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print(
            f"  {r['outreach_status']:<12} {r['n']:>5} {r['avg_score']:>9} "
            f"{r['had_email']:>9} {r['had_phone']:>9} {r['had_website']:>7}"
        )


def report_suggested_adjustments(
    rows: list[dict],
    conn: sqlite3.Connection,
    date_filter: str,
) -> None:
    print_section("Suggested Weight Adjustments")

    if not rows:
        print("  No segment data available for comparison.")
        return

    # Compute overall conversion rate
    sql = f"""
        SELECT COUNT(*) as total,
               SUM(outreach_status = 'CONVERTED') as converted
        FROM leads
        WHERE outreach_status != 'NEW'
          {date_filter};
    """
    overall = conn.execute(sql).fetchone()
    overall_total     = overall["total"] or 0
    overall_converted = overall["converted"] or 0

    if overall_total == 0:
        print("  No overall outcome data available.")
        return

    overall_rate = overall_converted / overall_total if overall_total > 0 else 0.0
    print(f"  Overall conversion rate: {overall_converted}/{overall_total} = {overall_rate:.1%}")
    print()

    # Aggregate by industry_category
    industry_stats: dict[str, dict] = {}
    for r in rows:
        cat = r.get("industry_category") or "unknown"
        if cat not in industry_stats:
            industry_stats[cat] = {"leads": 0, "converted": 0}
        industry_stats[cat]["leads"]     += r.get("leads", 0) or 0
        industry_stats[cat]["converted"] += r.get("converted", 0) or 0

    suggestions_found = False
    for cat, stats in sorted(industry_stats.items()):
        total     = stats["leads"]
        converted = stats["converted"]
        if total == 0:
            continue
        rate = converted / total
        if overall_rate > 0:
            ratio = rate / overall_rate
        else:
            ratio = 0.0

        if ratio > 2.0:
            print(
                f"  Consider INCREASING weight for '{cat}': "
                f"conversion rate {rate:.1%} is {ratio:.1f}x overall ({overall_rate:.1%})"
            )
            suggestions_found = True
        elif ratio < 0.5 and total >= 3:
            print(
                f"  Consider DECREASING weight for '{cat}': "
                f"conversion rate {rate:.1%} is {ratio:.1f}x overall ({overall_rate:.1%})"
            )
            suggestions_found = True

    if not suggestions_found:
        print("  No significant anomalies detected (all industries within 0.5x–2x of overall rate).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GA lead pipeline scoring calibration feedback report"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Lookback window in days (default: 90, use 0 for all time)",
    )
    args = parser.parse_args()

    conn, date_filter = get_conn(args.days)

    lookback = f"last {args.days} days" if args.days > 0 else "all time"
    print(f"\nGA Lead Pipeline — Scoring Feedback Report ({lookback})")
    print(f"DB path: {DB_PATH}")

    segment_rows = report_conversion_by_segment(conn, date_filter)
    report_score_accuracy(conn, date_filter)
    report_suggested_adjustments(segment_rows, conn, date_filter)

    conn.close()
    print()


if __name__ == "__main__":
    main()
