"""
CLI entry point for the GA Payment Leads Pipeline.

Subcommands
-----------
extract     Parse PDFs already on disk → classify → initial score → DB
enrich      Website search + payment-stack detection + rescore (needs browser)
score       Recompute fit scores for all DB leads (no browser)
export      Write ranked CSV of scored leads
discover    Scan eCorp for new entities (needs browser, touches eCorp)
run-all     Full pipeline: discover → download → extract → enrich → score → export

Usage examples
--------------
# Process existing PDFs and run enrichment on them:
python src/pipeline.py extract
python src/pipeline.py enrich
python src/pipeline.py export --priority HOT

# Full daily run (asks for confirmation before hitting eCorp):
python src/pipeline.py run-all --days 1

# Re-export everything:
python src/pipeline.py export --priority ALL
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.classify import classify_entity, is_registered_agent_service
from src.db import DB_PATH, get_connection, init_db, upsert_lead
from src.enrich import enrich_batch
from src.extract_pdf import extract_filing_data
from src.score import score_lead

logger = logging.getLogger(__name__)

RAW_DIR    = Path(__file__).parent.parent / "data" / "raw"
EXPORT_DIR = Path(__file__).parent.parent / "data" / "exports"


# ---------------------------------------------------------------------------
# extract subcommand
# ---------------------------------------------------------------------------

def cmd_extract(args) -> None:
    """Parse PDFs on disk, classify, score (no payment data), upsert to DB."""
    init_db()
    conn = get_connection(DB_PATH)

    pdfs = sorted(RAW_DIR.glob("*_formation.pdf"))
    if not pdfs:
        print(f"[extract] No formation PDFs found in {RAW_DIR}")
        print("         Run 'python src/pipeline.py discover' first to download PDFs.")
        return

    print(f"[extract] Found {len(pdfs)} formation PDF(s)")

    inserted = updated = skipped = errors = 0
    for pdf_path in pdfs:
        # Derive control_number from filename: "26041380_formation.pdf" → "26041380"
        control_number = pdf_path.stem.split("_")[0]
        try:
            data = extract_filing_data(pdf_path)

            entity_name = data.get("entity_name") or ""
            if not entity_name:
                print(f"  [skip] {pdf_path.name}: could not extract entity name")
                skipped += 1
                continue

            # Convert effective_date (MM/DD/YYYY) → ISO date
            formation_date = _parse_date(data.get("effective_date"))

            # Classify
            classification = classify_entity({
                "entity_name": entity_name,
                "naics_code":  "",
            })

            # Registered agent service check
            ra_name       = data.get("registered_agent_name") or ""
            ra_is_service = is_registered_agent_service(ra_name)

            entity_row = {
                "entity_name":                entity_name,
                "entity_type":                data.get("entity_type"),
                "formation_date":             str(formation_date) if formation_date else None,
                "organizer_name":             data.get("organizer_name"),
                "registered_agent_name":      ra_name,
                "registered_agent_address":   data.get("registered_agent_address"),
                "registered_agent_is_service": int(ra_is_service),
                "principal_office_address":   data.get("principal_office_address"),
                "filer_email":                data.get("filer_email"),
                "filer_phone":                data.get("filer_phone"),
            }

            # Initial score (no payment data yet)
            scored = score_lead(
                {**entity_row, "entity_name": entity_name},
                classification,
                payment_stack_data=None,
            )

            lead = {
                "control_number": control_number,
                **entity_row,
                "tier":               classification.get("tier"),
                "industry_category":  classification.get("industry_category"),
                "fit_score":          scored["fit_score"],
                "score_breakdown":    json.dumps(scored["score_breakdown"]),
                "priority":           scored["priority"],
            }

            existing = conn.execute(
                "SELECT control_number FROM leads WHERE control_number=?",
                (control_number,),
            ).fetchone()

            with conn:
                upsert_lead(conn, lead)

            action = "updated" if existing else "inserted"
            print(
                f"  [{action}] {entity_name[:45]:<45} "
                f"tier={classification['tier']}  "
                f"score={scored['fit_score']:>3}  "
                f"{scored['priority']}"
            )
            if existing:
                updated += 1
            else:
                inserted += 1

        except Exception as exc:
            print(f"  [error] {pdf_path.name}: {exc}")
            logger.exception("extract error for %s", pdf_path.name)
            errors += 1

    conn.close()
    total = inserted + updated
    print(
        f"\n[extract] Done — {inserted} inserted, {updated} updated, "
        f"{skipped} skipped, {errors} errors"
    )
    print(f"          Eligible for enrichment: {total - skipped} lead(s)")


# ---------------------------------------------------------------------------
# enrich subcommand
# ---------------------------------------------------------------------------

def cmd_enrich(args) -> None:
    """Run website search + payment-stack detection for un-enriched leads."""
    init_db()
    conn = get_connection(DB_PATH)

    pending = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE has_website IS NULL AND tier IS NOT NULL"
    ).fetchone()[0]

    if pending == 0:
        print("[enrich] No leads pending enrichment.")
        conn.close()
        return

    print(f"[enrich] {pending} lead(s) to enrich (launching browser for Bing search…)")

    limit = getattr(args, "limit", None)

    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--window-position=3000,3000"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        enriched, errors = enrich_batch(conn, browser_page=page, limit=limit)
        browser.close()

    conn.close()
    print(f"\n[enrich] Done — {enriched} enriched, {errors} errors")


# ---------------------------------------------------------------------------
# score subcommand
# ---------------------------------------------------------------------------

def cmd_score(args) -> None:
    """Recompute fit scores for all leads (e.g. after changing scoring weights)."""
    init_db()
    conn = get_connection(DB_PATH)

    rows = conn.execute("SELECT * FROM leads").fetchall()
    if not rows:
        print("[score] No leads in DB.")
        conn.close()
        return

    print(f"[score] Rescoring {len(rows)} lead(s)…")
    rescored = 0
    for row in rows:
        lead = dict(row)
        classification = {
            "tier":              lead.get("tier"),
            "match_source":      "",
            "industry_category": lead.get("industry_category"),
        }
        payment_data = None
        if lead.get("has_website") is not None:
            payment_data = {
                "has_website":                bool(lead.get("has_website")),
                "has_online_payment":         bool(lead.get("has_online_payment")),
                "has_online_booking":         bool(lead.get("has_online_booking")),
                "detected_payment_processor": lead.get("detected_payment_processor"),
                "detected_vertical_saas":     lead.get("detected_vertical_saas"),
                "invoice_workflow_signals":   bool(lead.get("invoice_workflow_signals")),
            }
        scored = score_lead(lead, classification, payment_data)
        with conn:
            conn.execute(
                """UPDATE leads SET
                    fit_score=?, score_breakdown=?, priority=?,
                    last_updated=CURRENT_TIMESTAMP
                WHERE control_number=?""",
                (
                    scored["fit_score"],
                    json.dumps(scored["score_breakdown"]),
                    scored["priority"],
                    lead["control_number"],
                ),
            )
        rescored += 1

    conn.close()
    print(f"[score] Done — {rescored} lead(s) rescored")


# ---------------------------------------------------------------------------
# export subcommand
# ---------------------------------------------------------------------------

def cmd_export(args) -> None:
    """Export scored leads to CSV, sorted by fit_score descending."""
    init_db()
    conn = get_connection(DB_PATH)

    priority_filter = getattr(args, "priority", "ALL")
    if priority_filter == "ALL":
        rows = conn.execute(
            "SELECT * FROM leads WHERE priority != 'SKIP' ORDER BY fit_score DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads WHERE priority=? ORDER BY fit_score DESC",
            (priority_filter,),
        ).fetchall()

    conn.close()

    if not rows:
        print(f"[export] No leads matching priority={priority_filter}")
        return

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = EXPORT_DIR / f"leads_{priority_filter.lower()}_{ts}.csv"

    EXPORT_COLS = [
        "control_number", "entity_name", "entity_type", "formation_date",
        "tier", "industry_category", "fit_score", "priority",
        "organizer_name", "filer_email", "filer_phone",
        "principal_office_address",
        "registered_agent_name", "registered_agent_is_service",
        "website", "has_website", "has_online_payment",
        "detected_payment_processor", "detected_vertical_saas",
        "invoice_workflow_signals",
        "outreach_status",
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))

    print(f"[export] {len(rows)} lead(s) → {out_csv}")

    # Summary table to stdout
    print(f"\n{'Priority':<8} {'Score':>5}  {'Entity':<45} {'Processor':<12} {'Website'}")
    print("-" * 100)
    for row in rows[:20]:
        r = dict(row)
        print(
            f"{r.get('priority','?'):<8} {r.get('fit_score',0):>5}  "
            f"{(r.get('entity_name') or '')[:45]:<45} "
            f"{(r.get('detected_payment_processor') or '—'):<12} "
            f"{r.get('website') or '—'}"
        )
    if len(rows) > 20:
        print(f"… and {len(rows) - 20} more rows in {out_csv.name}")


# ---------------------------------------------------------------------------
# discover subcommand  (human checkpoint — prompts for confirmation)
# ---------------------------------------------------------------------------

def cmd_discover(args) -> None:
    """Scan eCorp for new entities.  Prompts before hitting eCorp."""
    days = getattr(args, "days", 1)
    end_date   = date.today()
    start_date = end_date - timedelta(days=days - 1)

    print(
        f"\n[discover] About to scan eCorp for entities formed {start_date} – {end_date}."
        f"\n           This will make real HTTP requests to ecorp.sos.ga.gov"
        f"\n           at 1.5-second intervals using a real browser."
        f"\n           Max entities per run: 500 IDs probed."
    )
    confirm = input("\n  Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("[discover] Aborted.")
        return

    init_db()
    from src.discover import discover_new_entities
    entities = discover_new_entities(start_date, end_date)

    print(f"[discover] Found {len(entities)} new entity/entities in date range.")
    for e in entities:
        print(f"  {e.get('entity_name','?'):<45} formed={e.get('formation_date','?')}")


# ---------------------------------------------------------------------------
# run-all subcommand
# ---------------------------------------------------------------------------

def cmd_run_all(args) -> None:
    """Full pipeline: discover → download → extract → enrich → score → export."""
    days = getattr(args, "days", 1)

    print(f"\n[run-all] Full pipeline for last {days} day(s).")
    print("[run-all] Step 1/6: discover new entities")
    cmd_discover(args)

    print("\n[run-all] Step 2/6: download PDFs  (skipped — run download_pdfs.py separately for now)")

    print("\n[run-all] Step 3/6: extract")
    cmd_extract(args)

    print("\n[run-all] Step 4/6: enrich")
    cmd_enrich(args)

    print("\n[run-all] Step 5/6: score")
    cmd_score(args)

    print("\n[run-all] Step 6/6: export")
    args.priority = "ALL"
    cmd_export(args)

    print("\n[run-all] Complete.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: str | None) -> date | None:
    """Parse MM/DD/YYYY or YYYY-MM-DD into a date object."""
    if not date_str:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="GA Payment Leads Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # extract
    sub.add_parser("extract", help="Parse PDFs on disk → classify → DB")

    # enrich
    enrich_p = sub.add_parser("enrich", help="Website search + payment-stack detection")
    enrich_p.add_argument("--limit", type=int, default=None, help="Max leads to enrich")

    # score
    sub.add_parser("score", help="Recompute fit scores for all DB leads")

    # export
    exp_p = sub.add_parser("export", help="Export leads to CSV")
    exp_p.add_argument(
        "--priority", choices=["HOT", "WARM", "COLD", "ALL"], default="ALL"
    )

    # discover
    disc_p = sub.add_parser("discover", help="Scan eCorp for new entities (touches eCorp)")
    disc_p.add_argument("--days", type=int, default=1)

    # run-all
    run_p = sub.add_parser("run-all", help="Full pipeline end-to-end")
    run_p.add_argument("--days", type=int, default=1)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "extract":  cmd_extract,
        "enrich":   cmd_enrich,
        "score":    cmd_score,
        "export":   cmd_export,
        "discover": cmd_discover,
        "run-all":  cmd_run_all,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
