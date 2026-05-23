"""
Deduplication script for the GA lead generation pipeline.

Finds and optionally removes duplicate lead rows by entity_name
(case-insensitive) and by control_number.

Keeper selection priority (for entity_name duplicates):
  1. Highest fit_score
  2. If tied: most non-null fields
  3. If still tied: most recent last_updated

Runs in dry-run mode by default; use --execute to actually delete rows.

Usage:
    python3 scripts/dedup.py              # dry run — shows what would be deleted
    python3 scripts/dedup.py --execute    # actually deletes duplicates
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
DB_PATH = PROJECT / "data" / "leads.db"


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


def _non_null_count(conn: sqlite3.Connection, control_number: str) -> int:
    """Count non-null fields for a given lead row."""
    row = conn.execute(
        "SELECT * FROM leads WHERE control_number = ?", (control_number,)
    ).fetchone()
    if row is None:
        return 0
    return sum(1 for k in row.keys() if row[k] is not None)


def _choose_keeper(
    conn: sqlite3.Connection, cns: list[str]
) -> tuple[str, list[str]]:
    """From a list of control_numbers, pick the keeper and return (keeper, to_delete)."""
    candidates = []
    for cn in cns:
        row = conn.execute(
            "SELECT control_number, fit_score, last_updated FROM leads WHERE control_number = ?",
            (cn,),
        ).fetchone()
        if row is None:
            continue
        score        = row["fit_score"] if row["fit_score"] is not None else -1
        nn           = _non_null_count(conn, cn)
        last_updated = row["last_updated"] or ""
        candidates.append((score, nn, last_updated, cn))

    if not candidates:
        return cns[0], cns[1:]

    # Sort descending: highest score, then most fields, then latest update
    candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    keeper    = candidates[0][3]
    to_delete = [c[3] for c in candidates[1:]]
    return keeper, to_delete


def find_entity_name_dupes(conn: sqlite3.Connection) -> list[dict]:
    """Return duplicate groups keyed by lower(entity_name)."""
    sql = """
        SELECT lower(entity_name) as name_lower, COUNT(*) as n,
               GROUP_CONCAT(control_number) as cns
        FROM leads
        GROUP BY name_lower
        HAVING n > 1
        ORDER BY n DESC;
    """
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def find_control_number_dupes(conn: sqlite3.Connection) -> list[dict]:
    """Return duplicate groups by control_number (should be empty after dedup)."""
    sql = """
        SELECT control_number, COUNT(*) as n
        FROM leads
        GROUP BY control_number
        HAVING n > 1;
    """
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def process_entity_name_dupes(
    conn: sqlite3.Connection,
    groups: list[dict],
    execute: bool,
) -> tuple[int, int]:
    """Process duplicate entity_name groups.

    Returns (groups_processed, rows_deleted).
    """
    total_groups  = len(groups)
    total_deleted = 0

    for g in groups:
        name_lower = g["name_lower"]
        cns_str    = g["cns"] or ""
        cns        = [c.strip() for c in cns_str.split(",") if c.strip()]
        if len(cns) < 2:
            continue

        keeper, to_delete = _choose_keeper(conn, cns)

        for cn in to_delete:
            row = conn.execute(
                "SELECT entity_name, fit_score, last_updated FROM leads WHERE control_number = ?",
                (cn,),
            ).fetchone()
            entity_name  = (row["entity_name"] if row else name_lower)[:50]
            fit_score    = row["fit_score"] if row else "?"
            last_updated = row["last_updated"] if row else "?"

            if execute:
                conn.execute("DELETE FROM leads WHERE control_number = ?", (cn,))
                print(
                    f"  DELETED  control_number={cn}  entity={entity_name!r}"
                    f"  score={fit_score}  updated={last_updated}"
                )
            else:
                print(
                    f"  DRY-RUN  Would delete control_number={cn}  entity={entity_name!r}"
                    f"  score={fit_score}  updated={last_updated}"
                    f"  (keeper={keeper})"
                )
            total_deleted += 1

    if execute:
        conn.commit()

    return total_groups, total_deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate leads table by entity_name and control_number"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete duplicate rows (default: dry run only)",
    )
    args = parser.parse_args()

    conn = get_conn()

    mode_label = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\nGA Lead Pipeline — Dedup Script ({mode_label})")
    print(f"DB path: {DB_PATH}")

    # --- Entity name duplicates ---
    print_section("Duplicate entity_name Groups")
    entity_dupes = find_entity_name_dupes(conn)

    if not entity_dupes:
        print("  No duplicate entity names found.")
        total_groups  = 0
        total_deleted = 0
    else:
        total_extra = sum(g["n"] - 1 for g in entity_dupes)
        print(
            f"  Found {len(entity_dupes)} duplicate group(s) "
            f"({total_extra} total duplicate rows)."
        )
        if not args.execute:
            print("  Run with --execute to clean.\n")
        total_groups, total_deleted = process_entity_name_dupes(
            conn, entity_dupes, execute=args.execute
        )

    # --- Control number duplicates ---
    print_section("Duplicate control_number Check")
    cn_dupes = find_control_number_dupes(conn)
    if not cn_dupes:
        print("  No duplicate control_numbers found (expected).")
    else:
        print(f"  WARNING: {len(cn_dupes)} duplicate control_number(s) found!")
        for g in cn_dupes:
            print(f"    control_number={g['control_number']}  count={g['n']}")

    # --- Summary ---
    print_section("Summary")
    if entity_dupes:
        total_extra = sum(g["n"] - 1 for g in entity_dupes)
        print(
            f"  Found {len(entity_dupes)} duplicate group(s) "
            f"({total_extra} total duplicate rows)."
        )
        if args.execute:
            print(f"  Deleted {total_deleted} duplicate row(s).")
        else:
            print(f"  Run with --execute to clean.")
    else:
        print("  No duplicates found — database is clean.")

    conn.close()
    print()


if __name__ == "__main__":
    main()
