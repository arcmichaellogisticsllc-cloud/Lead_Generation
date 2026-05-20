"""
Backfill the GA leads pipeline for the last N days.

Phases
------
1. Discover  — Scan eCorp for new entities (real browser, touches eCorp)
2. Upsert    — Classify + insert basic entity rows into DB
3. Download  — Pull filing PDFs for tier-eligible entities (real browser, eCorp)
4. Extract   — Parse PDFs + update DB (no browser)
5. Enrich    — Website search + payment-stack detection (browser, Bing)
6. Score     — Final fit-score computation (no browser)
7. Export    — Write ranked CSV

HUMAN CHECKPOINT — reads "yes" confirmation before any eCorp requests.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.classify import classify_entity, is_registered_agent_service
from src.db import DB_PATH, get_connection, init_db, upsert_lead
from src.discover import discover_new_entities
from src.download_pdfs import download_entity_pdfs
from src.enrich import enrich_batch, _extract_city
from src.extract_pdf import extract_filing_data
from src.score import score_lead

LOG_PATH = Path(__file__).parent.parent / "data" / "pipeline.log"
RAW_DIR  = Path(__file__).parent.parent / "data" / "raw"
EXPORT_DIR = Path(__file__).parent.parent / "data" / "exports"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap GA leads pipeline for the last N days.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Number of days back to scan (default: 7)",
    )
    parser.add_argument(
        "--skip-discover", action="store_true",
        help="Skip discover+download phases (use PDFs already on disk)",
    )
    parser.add_argument(
        "--skip-enrich", action="store_true",
        help="Skip enrichment phase (website/payment-stack detection)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show plan only — do not execute",
    )
    args = parser.parse_args()

    end_date   = date.today()
    start_date = end_date - timedelta(days=args.days - 1)

    _print_plan(args, start_date, end_date)

    if args.dry_run:
        print("\n[dry-run] No actions taken.")
        return

    print("\n" + "=" * 60)
    print("  HUMAN CHECKPOINT")
    print("=" * 60)
    print(f"\n  This will make real requests to ecorp.sos.ga.gov")
    print(f"  for entities filed between {start_date} and {end_date}.")
    print(f"  Estimated time: {_estimate_time(args)} minutes.")
    print(f"  Rate limit: 1.5 s between eCorp page loads.")
    print()
    answer = input("  Type 'yes' to proceed, anything else to abort: ").strip().lower()
    if answer != "yes":
        print("\n[aborted] No requests were made.")
        sys.exit(0)

    print()
    init_db()
    conn    = get_connection(DB_PATH)
    run_log = {
        "start_time":          datetime.now().isoformat(),
        "date_range_start":    str(start_date),
        "date_range_end":      str(end_date),
        "entities_discovered": 0,
        "entities_kept":       0,
        "pdfs_downloaded":     0,
        "enrichment_completed": 0,
        "errors":              [],
    }

    try:
        if not args.skip_discover:
            _phase_discover(conn, run_log, start_date, end_date)
            _phase_download(conn, run_log)
        else:
            print("[bootstrap] Skipping discover/download phases.")

        _phase_extract(conn, run_log)

        if not args.skip_enrich:
            _phase_enrich(conn, run_log)
        else:
            print("[bootstrap] Skipping enrich phase.")

        _phase_score(conn, run_log)
        _phase_export(conn, run_log)

    except KeyboardInterrupt:
        print("\n[bootstrap] Interrupted by user.")
    finally:
        _record_run(conn, run_log)
        conn.close()

    _print_summary(run_log)


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _phase_discover(conn, run_log: dict, start_date: date, end_date: date) -> None:
    print(f"\n{'='*60}")
    print(f"  Phase 1/6: Discover  ({start_date} – {end_date})")
    print(f"{'='*60}")

    state_file = Path(__file__).parent.parent / "state.json"

    def _watermark() -> str:
        try:
            return json.loads(state_file.read_text()).get("watermark", "0")
        except Exception:
            return "0"

    all_entities: list[dict] = []
    MAX_PASSES = 12   # safety cap (12 × 500 = 6000 IDs max)

    # Discover with a wide date window (full year-to-date) so that the
    # per-entity date filter inside discover doesn't silently drop 2026
    # entities formed outside the narrow bootstrap window.  The user's
    # --days window is applied later at export / weekly-report time.
    discover_start = date(start_date.year, 1, 1)
    discover_end   = end_date

    for pass_num in range(1, MAX_PASSES + 1):
        wm_before = _watermark()
        print(f"  Pass {pass_num}: watermark={wm_before} …")
        batch = discover_new_entities(discover_start, discover_end)
        wm_after = _watermark()
        print(f"           → watermark={wm_after}  found {len(batch)} matching entities")
        all_entities.extend(batch)

        if wm_after == wm_before:
            print(f"  Frontier reached — watermark unchanged after pass {pass_num}.")
            break

    existing = {
        row[0]
        for row in conn.execute("SELECT control_number FROM leads").fetchall()
    }

    run_log["entities_discovered"] = len(all_entities)

    kept = 0
    for entity in all_entities:
        control_number = entity.get("control_number")
        if not control_number or control_number in existing:
            continue

        entity_name = entity.get("entity_name") or ""
        cl = classify_entity({"entity_name": entity_name, "naics_code": ""})
        ra_name       = entity.get("registered_agent_name") or ""
        ra_is_service = is_registered_agent_service(ra_name)

        lead = {
            "control_number":              control_number,
            "entity_name":                 entity_name,
            "entity_type":                 entity.get("entity_type"),
            "status":                      entity.get("status"),
            "formation_date":              entity.get("formation_date"),
            "tier":                        cl.get("tier"),
            "industry_category":           cl.get("industry_category"),
            "registered_agent_name":       ra_name,
            "registered_agent_is_service": int(ra_is_service),
        }
        with conn:
            upsert_lead(conn, lead)
        kept += 1
        existing.add(control_number)

    run_log["entities_kept"] = kept
    print(f"  → Total discovered {len(all_entities)}, kept {kept} new entities in DB")


def _phase_download(conn, run_log: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  Phase 2/6: Download PDFs")
    print(f"{'='*60}")

    # Only download for tier-eligible entities without existing PDFs
    rows = conn.execute(
        "SELECT control_number, entity_name FROM leads WHERE tier IS NOT NULL"
    ).fetchall()

    to_download = []
    for row in rows:
        cn  = row["control_number"]
        pdf = RAW_DIR / f"{cn}_formation.pdf"
        if not pdf.exists():
            to_download.append(dict(row))

    if not to_download:
        print("  → All eligible entities already have PDFs on disk.")
        return

    print(f"  → {len(to_download)} entity/entities need PDFs")

    from patchright.sync_api import sync_playwright
    from src.discover import BASE_URL, USER_AGENT

    downloaded = 0
    errors = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--window-position=3000,3000"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())

        # Warm up: navigate to eCorp base once
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
            time.sleep(1)
        except Exception as exc:
            logger.warning("eCorp warm-up navigation failed: %s", exc)

        for entity in to_download:
            cn = entity["control_number"]
            try:
                pdfs = download_entity_pdfs(page, cn, cn)
                downloaded += len(pdfs)
                print(
                    f"  ✓ {entity['entity_name'][:50]:<50} "
                    f"{len(pdfs)} PDF(s)"
                )
                time.sleep(1.5)
            except Exception as exc:
                logger.error("Download failed for %s: %s", cn, exc)
                run_log["errors"].append(f"download:{cn}:{exc}")
                errors += 1

        browser.close()

    run_log["pdfs_downloaded"] = downloaded
    print(f"  → Downloaded {downloaded} PDF(s), {errors} errors")


def _phase_extract(conn, run_log: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  Phase 3/6: Extract PDFs + Classify + Initial Score")
    print(f"{'='*60}")

    pdfs = sorted(RAW_DIR.glob("*_formation.pdf"))
    print(f"  → {len(pdfs)} formation PDF(s) on disk")

    processed = 0
    for pdf_path in pdfs:
        control_number = pdf_path.stem.split("_")[0]
        try:
            data = extract_filing_data(pdf_path)
            entity_name = data.get("entity_name") or ""
            if not entity_name:
                continue

            formation_date = _parse_date_str(data.get("effective_date"))
            cl = classify_entity({"entity_name": entity_name, "naics_code": ""})
            ra_name = data.get("registered_agent_name") or ""
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
            scored = score_lead(
                {**entity_row, "entity_name": entity_name},
                cl,
                payment_stack_data=None,
            )
            lead = {
                "control_number": control_number,
                **entity_row,
                "tier":              cl.get("tier"),
                "industry_category": cl.get("industry_category"),
                "fit_score":         scored["fit_score"],
                "score_breakdown":   json.dumps(scored["score_breakdown"]),
                "priority":          scored["priority"],
            }
            with conn:
                upsert_lead(conn, lead)
            processed += 1
        except Exception as exc:
            logger.error("Extract error %s: %s", pdf_path.name, exc)
            run_log["errors"].append(f"extract:{pdf_path.name}:{exc}")

    print(f"  → Processed {processed} PDF(s)")


def _phase_enrich(conn, run_log: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  Phase 4/6: Enrich (website + payment stack)")
    print(f"{'='*60}")

    pending = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE has_website IS NULL AND tier IS NOT NULL"
    ).fetchone()[0]

    if pending == 0:
        print("  → No leads pending enrichment.")
        return

    print(f"  → Enriching {pending} lead(s) (launching Bing search browser…)")

    from patchright.sync_api import sync_playwright
    from src.discover import USER_AGENT

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--window-position=3000,3000"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        enriched, errors = enrich_batch(conn, browser_page=page)
        browser.close()

    run_log["enrichment_completed"] = enriched
    if errors:
        run_log["errors"].append(f"enrich:{errors} errors")
    print(f"  → Enriched {enriched}, {errors} errors")


def _phase_score(conn, run_log: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  Phase 5/6: Rescore all leads")
    print(f"{'='*60}")

    rows = conn.execute("SELECT * FROM leads").fetchall()
    rescored = 0
    for row in rows:
        lead = dict(row)
        cl = {
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
        scored = score_lead(lead, cl, payment_data)
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

    print(f"  → Rescored {rescored} leads")


def _phase_export(conn, run_log: dict) -> None:
    import csv
    print(f"\n{'='*60}")
    print(f"  Phase 6/6: Export CSV")
    print(f"{'='*60}")

    rows = conn.execute(
        "SELECT * FROM leads WHERE priority != 'SKIP' ORDER BY fit_score DESC"
    ).fetchall()

    if not rows:
        print("  → No exportable leads (all SKIP).")
        return

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = EXPORT_DIR / f"bootstrap_{ts}.csv"

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

    run_log["export_path"] = str(out_csv)
    print(f"  → {len(rows)} lead(s) → {out_csv}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_plan(args, start_date: date, end_date: date) -> None:
    print()
    print("=" * 60)
    print("  GA Payment Leads — Bootstrap Plan")
    print("=" * 60)
    print(f"  Date range : {start_date}  →  {end_date}  ({args.days} day(s))")
    print(f"  DB path    : {DB_PATH}")
    print(f"  PDF dir    : {RAW_DIR}")
    print(f"  Log        : {LOG_PATH}")
    print()
    phases = [
        ("1", "Discover",  "Scan eCorp businessIds sequentially (patchright)", not args.skip_discover),
        ("2", "Download",  "Pull formation PDFs for tier-eligible entities",   not args.skip_discover),
        ("3", "Extract",   "Parse PDFs → classify → initial score",            True),
        ("4", "Enrich",    "Bing search → payment-stack detection → rescore",  not args.skip_enrich),
        ("5", "Score",     "Final fit-score pass",                             True),
        ("6", "Export",    "Write ranked CSV to data/exports/",                True),
    ]
    for num, name, desc, enabled in phases:
        mark = "✓" if enabled else "—"
        print(f"  [{mark}] Phase {num}: {name:<10} {desc}")
    print()
    print(f"  Estimated time: ~{_estimate_time(args)} min  (1.5 s eCorp delay, 2 s Bing delay)")


def _estimate_time(args) -> int:
    if args.skip_discover:
        return 5
    scan_per_pass = int(__import__("os").environ.get("DISCOVER_SCAN", "500"))
    passes        = max(1, args.days // 2)
    discover_min  = passes * (scan_per_pass * 1.5 / 60)
    download_min  = 3    # ~40 PDFs × 4 s
    enrich_min    = 5 if not args.skip_enrich else 0
    return int(discover_min + download_min + enrich_min) + 2


def _record_run(conn, run_log: dict) -> None:
    try:
        with conn:
            conn.execute(
                """INSERT INTO pipeline_runs
                    (date_range_start, date_range_end, entities_discovered,
                     entities_kept, pdfs_downloaded, enrichment_completed, errors)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    run_log.get("date_range_start"),
                    run_log.get("date_range_end"),
                    run_log.get("entities_discovered", 0),
                    run_log.get("entities_kept", 0),
                    run_log.get("pdfs_downloaded", 0),
                    run_log.get("enrichment_completed", 0),
                    json.dumps(run_log.get("errors", [])),
                ),
            )
    except Exception as exc:
        logger.warning("Could not record pipeline run: %s", exc)


def _print_summary(run_log: dict) -> None:
    print()
    print("=" * 60)
    print("  Bootstrap Complete — Summary")
    print("=" * 60)
    print(f"  Discovered  : {run_log.get('entities_discovered', 0)} total entities")
    print(f"  Kept        : {run_log.get('entities_kept', 0)} new DB rows")
    print(f"  PDFs        : {run_log.get('pdfs_downloaded', 0)} downloaded")
    print(f"  Enriched    : {run_log.get('enrichment_completed', 0)} leads")
    errors = run_log.get("errors", [])
    print(f"  Errors      : {len(errors)}")
    if run_log.get("export_path"):
        print(f"  CSV export  : {run_log['export_path']}")
    print()


def _parse_date_str(date_str: str | None):
    """Parse MM/DD/YYYY or YYYY-MM-DD → date."""
    if not date_str:
        return None
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            pass
    return None


if __name__ == "__main__":
    main()
