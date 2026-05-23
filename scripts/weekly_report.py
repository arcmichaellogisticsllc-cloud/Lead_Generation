"""
Generate a weekly markdown summary of new leads.

Queries leads created in the last 7 days, groups by tier and priority,
and prints a formatted report to stdout + saves to data/exports/.

Usage:
  python scripts/weekly_report.py
  python scripts/weekly_report.py --days 14   # look back further
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import DB_PATH, get_connection, init_db

EXPORT_DIR = Path(__file__).parent.parent / "data" / "exports"


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly leads report")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    args = parser.parse_args()

    init_db()
    conn = get_connection(DB_PATH)
    cutoff = date.today() - timedelta(days=args.days)

    rows = conn.execute(
        "SELECT * FROM leads WHERE first_seen >= ? ORDER BY fit_score DESC",
        (str(cutoff),),
    ).fetchall()
    conn.close()

    report = _build_report(rows, args.days, cutoff)
    print(report)

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EXPORT_DIR / f"weekly_{date.today().isoformat()}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n[saved to {out_path}]", file=sys.stderr)


def _build_report(rows, days: int, cutoff: date) -> str:
    today = date.today()
    lines: list[str] = []
    lines.append(f"# GA Payment Leads — Weekly Report")
    lines.append(f"**Generated:** {today}  |  **Period:** {cutoff} – {today} ({days} days)")
    lines.append("")

    if not rows:
        lines.append("_No new leads in this period._")
        return "\n".join(lines)

    # ---------- Summary table ----------
    priority_counts: dict[str, int] = {"HOT": 0, "WARM": 0, "COLD": 0, "SKIP": 0}
    tier_counts:     dict[str, int] = {"1": 0, "2": 0, "3": 0, "None": 0}

    for row in rows:
        p = row["priority"] or "SKIP"
        priority_counts[p] = priority_counts.get(p, 0) + 1
        t = str(row["tier"]) if row["tier"] else "None"
        tier_counts[t] = tier_counts.get(t, 0) + 1

    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total new leads | {len(rows)} |")
    lines.append(f"| 🔥 HOT  | {priority_counts.get('HOT', 0)} |")
    lines.append(f"| 🟡 WARM | {priority_counts.get('WARM', 0)} |")
    lines.append(f"| 🔵 COLD | {priority_counts.get('COLD', 0)} |")
    lines.append(f"| ⬜ SKIP | {priority_counts.get('SKIP', 0)} |")
    lines.append(f"| Tier 1 (trades) | {tier_counts.get('1', 0)} |")
    lines.append(f"| Tier 2 (auto)   | {tier_counts.get('2', 0)} |")
    lines.append(f"| Tier 3 (wellness/cleaning) | {tier_counts.get('3', 0)} |")
    lines.append("")

    # ---------- Top leads ----------
    top = [r for r in rows if (r["priority"] or "SKIP") not in ("SKIP",)][:20]
    def _md_cell(s: str) -> str:
        return (s or "").replace("|", "\\|").replace("\n", " ").replace("\r", "")

    if top:
        lines.append("## Top Leads (non-SKIP)")
        lines.append("")
        lines.append("| Score | Priority | Entity | Category | Website | Processor |")
        lines.append("|------:|----------|--------|----------|---------|-----------|")
        for row in top:
            name      = _md_cell((row["entity_name"] or "")[:45])
            cat       = _md_cell((row["industry_category"] or "—")[:20])
            website   = _md_cell((row["website"] or "—")[:40])
            processor = _md_cell(row["detected_payment_processor"] or "—")
            lines.append(
                f"| {row['fit_score'] or 0:>3} "
                f"| {row['priority'] or '?'} "
                f"| {name} "
                f"| {cat} "
                f"| {website} "
                f"| {processor} |"
            )
        lines.append("")

    # ---------- HOT leads detail ----------
    hot = [r for r in rows if r["priority"] == "HOT"]
    if hot:
        lines.append("## HOT Leads — Contact Details")
        lines.append("")
        for row in hot:
            lines.append(f"### {row['entity_name']}")
            lines.append(f"- **Score:** {row['fit_score']}  **Category:** {row['industry_category'] or '—'}")
            lines.append(f"- **Organizer:** {row['organizer_name'] or '—'}")
            lines.append(f"- **Address:** {row['principal_office_address'] or '—'}")
            lines.append(f"- **Email:** {row['filer_email'] or '—'}")
            lines.append(f"- **Phone:** {row['filer_phone'] or '—'}")
            lines.append(f"- **Website:** {row['website'] or '—'}")
            lines.append(f"- **Payment processor:** {row['detected_payment_processor'] or '—'}")
            lines.append(f"- **Formed:** {row['formation_date'] or '—'}")
            lines.append("")

    # ---------- Pipeline stats ----------
    lines.append("## Pipeline Run Stats")
    lines.append("")
    lines.append(f"_Report generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
