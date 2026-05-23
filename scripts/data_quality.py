"""
Standalone diagnostic script for the GA lead generation pipeline.

Reads the DB at data/leads.db and prints a quality report covering:
  - Contact coverage by priority
  - Score distribution
  - Actionability (HOT/WARM with at least one contact method)
  - Dedup detection
  - Transmittal PDF coverage on disk

Usage:
    python3 scripts/data_quality.py
    python3 scripts/data_quality.py --fix-dupes
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
DB_PATH = PROJECT / "data" / "leads.db"
RAW_DIR = PROJECT / "data" / "raw"


def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def print_section(title: str) -> None:
    width = 60
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def report_contact_coverage(conn: sqlite3.Connection) -> None:
    print_section("Contact Coverage")
    sql = """
        SELECT
          priority,
          COUNT(*) as total,
          SUM(filer_email IS NOT NULL AND filer_email != '') as has_email,
          SUM(filer_phone IS NOT NULL AND filer_phone != '') as has_phone,
          SUM(organizer_name IS NOT NULL AND organizer_name != '') as has_organizer,
          SUM(principal_office_address IS NOT NULL) as has_address
        FROM leads
        WHERE priority IN ('HOT','WARM','COLD')
        GROUP BY priority ORDER BY priority;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No HOT/WARM/COLD leads found.")
        return

    header = f"{'Priority':<10} {'Total':>7} {'Email':>7} {'Phone':>7} {'Organizer':>10} {'Address':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        total = r["total"] or 1  # avoid division by zero in display
        print(
            f"{r['priority']:<10} {r['total']:>7} "
            f"{r['has_email']:>7} {r['has_phone']:>7} "
            f"{r['has_organizer']:>10} {r['has_address']:>8}"
        )


def report_score_distribution(conn: sqlite3.Connection) -> None:
    print_section("Score Distribution")
    sql = """
        SELECT priority, COUNT(*) as n,
               ROUND(AVG(fit_score), 1) as avg_score,
               MIN(fit_score) as min_score,
               MAX(fit_score) as max_score
        FROM leads
        WHERE priority != 'SKIP'
        GROUP BY priority;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No scored leads found.")
        return

    header = f"{'Priority':<10} {'Count':>7} {'Avg':>6} {'Min':>6} {'Max':>6}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['priority']:<10} {r['n']:>7} "
            f"{r['avg_score']:>6} {r['min_score']:>6} {r['max_score']:>6}"
        )


def report_actionability(conn: sqlite3.Connection) -> None:
    print_section("Actionability (HOT/WARM leads)")
    sql_with_contact = """
        SELECT COUNT(*) as n FROM leads
        WHERE priority IN ('HOT','WARM')
          AND (
            (filer_email IS NOT NULL AND filer_email != '')
            OR (filer_phone IS NOT NULL AND filer_phone != '')
            OR website IS NOT NULL
            OR yelp_url IS NOT NULL
            OR facebook_url IS NOT NULL
            OR google_maps_url IS NOT NULL
          );
    """
    sql_no_contact = """
        SELECT COUNT(*) as n FROM leads
        WHERE priority IN ('HOT','WARM')
          AND (filer_email IS NULL OR filer_email = '')
          AND (filer_phone IS NULL OR filer_phone = '')
          AND website IS NULL
          AND yelp_url IS NULL
          AND facebook_url IS NULL
          AND google_maps_url IS NULL;
    """
    with_contact = conn.execute(sql_with_contact).fetchone()["n"]
    no_contact   = conn.execute(sql_no_contact).fetchone()["n"]
    total        = with_contact + no_contact

    print(f"  HOT/WARM leads total       : {total}")
    print(f"  With at least one contact  : {with_contact}")
    print(f"  With NO contact method     : {no_contact}")
    if total > 0:
        pct = round(100.0 * with_contact / total, 1)
        print(f"  Actionability rate         : {pct}%")


def report_dedup(conn: sqlite3.Connection) -> None:
    print_section("Dedup Detection (duplicate entity_name)")
    sql = """
        SELECT entity_name, COUNT(*) as n
        FROM leads
        GROUP BY entity_name
        HAVING n > 1
        ORDER BY n DESC
        LIMIT 20;
    """
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("  No duplicate entity names found.")
        return

    total_dupes = sum(r["n"] - 1 for r in rows)
    print(f"  Found {len(rows)} duplicate group(s) ({total_dupes} extra rows in top-20):")
    print()
    header = f"  {'Entity Name':<50} {'Count':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        name = (r["entity_name"] or "")[:50]
        print(f"  {name:<50} {r['n']:>6}")


def report_transmittal_coverage(conn: sqlite3.Connection) -> None:
    print_section("Transmittal PDF Coverage")
    rows = conn.execute("SELECT control_number FROM leads").fetchall()
    total   = len(rows)
    present = 0
    missing = 0
    for r in rows:
        cn = r["control_number"]
        if cn and (RAW_DIR / f"{cn}_transmittal.pdf").exists():
            present += 1
        else:
            missing += 1

    print(f"  Total leads                : {total}")
    print(f"  With transmittal PDF       : {present}")
    print(f"  Without transmittal PDF    : {missing}")
    if total > 0:
        pct = round(100.0 * present / total, 1)
        print(f"  Transmittal coverage       : {pct}%")


def fix_duplicates(conn: sqlite3.Connection) -> None:
    print_section("Fixing Duplicates (--fix-dupes)")
    sql = """
        SELECT entity_name, COUNT(*) as n
        FROM leads
        GROUP BY entity_name
        HAVING n > 1;
    """
    groups = conn.execute(sql).fetchall()
    if not groups:
        print("  No duplicates found — nothing to do.")
        return

    deleted_total = 0
    for g in groups:
        name = g["entity_name"]
        # Find all rows for this entity_name, pick the keeper
        rows = conn.execute(
            "SELECT control_number, fit_score, last_updated FROM leads WHERE entity_name = ?",
            (name,),
        ).fetchall()

        # Count non-null fields as a tiebreaker
        def non_null_count(cn: str) -> int:
            row = conn.execute("SELECT * FROM leads WHERE control_number = ?", (cn,)).fetchone()
            if row is None:
                return 0
            return sum(1 for k in row.keys() if row[k] is not None)

        def sort_key(r):
            score        = r["fit_score"] if r["fit_score"] is not None else -1
            nn_count     = non_null_count(r["control_number"])
            last_updated = r["last_updated"] or ""
            return (score, nn_count, last_updated)

        sorted_rows = sorted(rows, key=sort_key, reverse=True)
        keeper = sorted_rows[0]["control_number"]
        to_delete = [r["control_number"] for r in sorted_rows[1:]]

        for cn in to_delete:
            conn.execute("DELETE FROM leads WHERE control_number = ?", (cn,))
            print(f"  Deleted duplicate: entity_name={name!r} control_number={cn}")
            deleted_total += 1

    conn.commit()
    print(f"\n  Done. Deleted {deleted_total} duplicate row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GA lead pipeline data quality diagnostic"
    )
    parser.add_argument(
        "--fix-dupes",
        action="store_true",
        help="Delete duplicate rows, keeping the one with highest fit_score / most data / latest update.",
    )
    args = parser.parse_args()

    conn = get_conn()

    print("\nGA Lead Pipeline — Data Quality Report")
    print(f"DB path : {DB_PATH}")
    print(f"RAW dir : {RAW_DIR}")

    report_contact_coverage(conn)
    report_score_distribution(conn)
    report_actionability(conn)
    report_dedup(conn)
    report_transmittal_coverage(conn)

    if args.fix_dupes:
        fix_duplicates(conn)

    conn.close()
    print()


if __name__ == "__main__":
    main()
