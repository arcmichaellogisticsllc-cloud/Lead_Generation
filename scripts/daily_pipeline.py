"""
Daily pipeline runner — called by launchd at 7 AM (full) and 1 PM (quick) on weekdays.

Runs unattended (no interactive prompts).
Logs everything to data/pipeline.log.
Sends a macOS notification when done.

Modes:
  full  (default) — all 6 stages, ~15–25 min
  quick           — stages 1–3 + 5 only (no enrich, no export), ~8–12 min

Stages:
  1. Discover  — scan up to DISCOVER_SCAN new eCorp businessIds
  2. Download  — pull PDFs for new tier-eligible entities
  3. Extract   — parse PDFs + classify + score
  4. Enrich    — website search + payment stack (Bing, patchright) [full only]
  5. Score     — final fit-score pass
  6. Export    — write ranked CSV [full only]

After scoring (both modes):
  - Auto-starts cadence for HOT leads < 48h old when AUTO_CADENCE_HOT=1
  - Fires per-lead macOS alert + optional Twilio SMS for new HOT leads

Lock file (data/.pipeline.lock) prevents overlapping runs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

LOG_PATH  = PROJECT / "data" / "pipeline.log"
LOCK_PATH = PROJECT / "data" / ".pipeline.lock"
RAW_DIR   = PROJECT / "data" / "raw"
EXPORT_DIR = PROJECT / "data" / "exports"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# IDs to scan per daily run (~8–12 min at 1.5 s/ID)
DAILY_SCAN = int(os.environ.get("DISCOVER_SCAN", "300"))

from src.cadence import CADENCE as _CADENCE  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GA Leads daily pipeline")
    parser.add_argument(
        "--mode",
        choices=["full", "quick"],
        default="full",
        help="full = all stages; quick = discover/download/extract/score only (no enrich/export)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    mode = args.mode

    if not _acquire_lock():
        logger.warning("Pipeline already running (lock file exists). Exiting.")
        sys.exit(0)

    run_log = {
        "start_time": datetime.now().isoformat(),
        "entities_discovered": 0,
        "entities_kept": 0,
        "pdfs_downloaded": 0,
        "enrichment_completed": 0,
        "new_hot": 0,
        "new_warm": 0,
        "errors": [],
    }

    try:
        _headless = os.environ.get("BROWSER_HEADLESS", "0") != "0"

        logger.info("=" * 50)
        logger.info("Daily pipeline starting — %s  [mode=%s]", date.today(), mode)
        logger.info("=" * 50)

        from src.db import DB_PATH, get_connection, init_db, upsert_lead
        from src.classify import classify_entity, is_registered_agent_service
        from src.discover import discover_new_entities
        from src.download_pdfs import download_entity_pdfs
        from src.enrich import enrich_batch
        from src.extract_pdf import extract_filing_data
        from src.score import score_lead

        init_db()
        conn = get_connection(DB_PATH)

        # ── 1. Discover ────────────────────────────────────────────────
        os.environ["DISCOVER_SCAN"] = str(DAILY_SCAN)
        end_date   = date.today()
        start_date = date(end_date.year, 1, 1)   # full year-to-date window

        logger.info("Phase 1: Discover (scan=%d, range=%s–%s)", DAILY_SCAN, start_date, end_date)
        entities = discover_new_entities(start_date, end_date)
        run_log["entities_discovered"] = len(entities)
        logger.info("  → %d new entities found", len(entities))

        existing = {r[0] for r in conn.execute("SELECT control_number FROM leads").fetchall()}
        kept = 0
        for entity in entities:
            cn = entity.get("control_number")
            if not cn or cn in existing:
                continue
            entity_name = entity.get("entity_name") or ""
            cl = classify_entity({"entity_name": entity_name, "naics_code": ""})
            ra_is_service = is_registered_agent_service(entity.get("registered_agent_name") or "")
            lead = {
                "control_number": cn,
                "entity_name":    entity_name,
                "entity_type":    entity.get("entity_type"),
                "status":         entity.get("status"),
                "formation_date": entity.get("formation_date"),
                "tier":           cl.get("tier"),
                "industry_category": cl.get("industry_category"),
                "registered_agent_name":       entity.get("registered_agent_name") or "",
                "registered_agent_is_service": int(ra_is_service),
            }
            with conn:
                upsert_lead(conn, lead)
            existing.add(cn)
            kept += 1
        run_log["entities_kept"] = kept

        # ── 2. Download PDFs ────────────────────────────────────────────
        to_download = []
        for entity in entities:
            cn = entity.get("control_number")
            if cn and entity.get("tier") is not None:
                if not (RAW_DIR / f"{cn}_formation.pdf").exists():
                    to_download.append(cn)

        downloaded = 0
        if to_download:
            logger.info("Phase 2: Download %d PDFs", len(to_download))
            from patchright.sync_api import sync_playwright
            from src.discover import BASE_URL, USER_AGENT
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=_headless,
                    args=([] if _headless else ["--window-position=3000,3000"]),
                )
                ctx  = browser.new_context(user_agent=USER_AGENT,
                                           viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                page.on("dialog", lambda d: d.accept())
                try:
                    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
                    time.sleep(1)
                except Exception:
                    pass
                for cn in to_download:
                    try:
                        pdfs = download_entity_pdfs(page, cn, cn)
                        downloaded += len(pdfs)
                        time.sleep(1.5)
                    except Exception as exc:
                        logger.error("  PDF download failed for %s: %s", cn, exc)
                        run_log["errors"].append(f"download:{cn}")
                browser.close()
        run_log["pdfs_downloaded"] = downloaded
        logger.info("Phase 2 done — %d PDF(s) downloaded", downloaded)

        # ── 3. Extract ──────────────────────────────────────────────────
        logger.info("Phase 3: Extract PDFs")
        for pdf_path in sorted(RAW_DIR.glob("*_formation.pdf")):
            cn = pdf_path.stem.split("_")[0]
            try:
                transmittal_path = RAW_DIR / f"{cn}_transmittal.pdf"
                data = extract_filing_data(
                    pdf_path,
                    transmittal_path=transmittal_path if transmittal_path.exists() else None,
                )
                entity_name = data.get("entity_name") or ""
                if not entity_name:
                    continue
                from scripts.bootstrap_historical import _parse_date_str
                formation_date = _parse_date_str(data.get("effective_date"))
                cl = classify_entity({"entity_name": entity_name, "naics_code": ""})
                ra_is_service = is_registered_agent_service(data.get("registered_agent_name") or "")
                entity_row = {
                    "entity_name": entity_name,
                    "entity_type": data.get("entity_type"),
                    "formation_date": str(formation_date) if formation_date else None,
                    "organizer_name": data.get("organizer_name"),
                    "registered_agent_name": data.get("registered_agent_name") or "",
                    "registered_agent_address": data.get("registered_agent_address"),
                    "registered_agent_is_service": int(ra_is_service),
                    "principal_office_address": data.get("principal_office_address"),
                    "filer_email": data.get("filer_email"),
                    "filer_phone": data.get("filer_phone"),
                }
                scored = score_lead({**entity_row, "entity_name": entity_name}, cl, None)
                lead = {
                    "control_number": cn,
                    **entity_row,
                    "tier": cl.get("tier"),
                    "industry_category": cl.get("industry_category"),
                    "fit_score": scored["fit_score"],
                    "score_breakdown": json.dumps(scored["score_breakdown"]),
                    "priority": scored["priority"],
                }
                with conn:
                    upsert_lead(conn, lead)
            except Exception as exc:
                logger.error("  Extract error %s: %s", pdf_path.name, exc)
                run_log["errors"].append(f"extract:{pdf_path.name}")

        # ── 4. Enrich ───────────────────────────────────────────────────
        enriched = 0
        if mode == "full":
            pending = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE has_website IS NULL AND tier IS NOT NULL"
            ).fetchone()[0]

            if pending:
                logger.info("Phase 4: Enrich %d lead(s)", pending)
                from patchright.sync_api import sync_playwright
                from src.discover import USER_AGENT
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=_headless,
                        args=([] if _headless else ["--window-position=3000,3000"]),
                    )
                    ctx  = browser.new_context(user_agent=USER_AGENT,
                                               viewport={"width": 1280, "height": 800})
                    page = ctx.new_page()
                    enriched, errs = enrich_batch(conn, browser_page=page)
                    browser.close()
                run_log["enrichment_completed"] = enriched
                if errs:
                    run_log["errors"].append(f"enrich:{errs} errors")
        else:
            logger.info("Phase 4: Enrich — skipped (quick mode)")

        # ── 5. Score ────────────────────────────────────────────────────
        logger.info("Phase 5: Final rescore")
        rows = conn.execute("SELECT * FROM leads").fetchall()
        for row in rows:
            lead = dict(row)
            cl   = {"tier": lead.get("tier"), "match_source": "",
                    "industry_category": lead.get("industry_category")}
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
                    "UPDATE leads SET fit_score=?, score_breakdown=?, priority=?, "
                    "last_updated=CURRENT_TIMESTAMP WHERE control_number=?",
                    (scored["fit_score"], json.dumps(scored["score_breakdown"]),
                     scored["priority"], lead["control_number"]),
                )

        # ── Post-score: auto-cadence + alerts ──────────────────────────
        today_str = str(date.today())
        cutoff_48h = str(datetime.now() - timedelta(hours=48))

        if os.environ.get("AUTO_CADENCE_HOT", "0") == "1":
            _auto_start_cadences(conn, cutoff_48h, today_str)

        _alert_hot_leads(conn, today_str, cutoff_48h, mode)

        # ── 6. Export ───────────────────────────────────────────────────
        if mode == "full":
            import csv
            logger.info("Phase 6: Export")
            today_rows = conn.execute(
                "SELECT * FROM leads WHERE priority != 'SKIP' ORDER BY fit_score DESC"
            ).fetchall()

            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            out_csv = EXPORT_DIR / f"daily_{date.today().isoformat()}.csv"
            COLS = ["control_number","entity_name","entity_type","formation_date","tier",
                    "industry_category","fit_score","priority","organizer_name",
                    "filer_email","filer_phone","principal_office_address",
                    "website","has_website","has_online_payment",
                    "detected_payment_processor","outreach_status"]
            with open(out_csv, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
                writer.writeheader()
                for row in today_rows:
                    writer.writerow(dict(row))
        else:
            logger.info("Phase 6: Export — skipped (quick mode)")
        new_hot  = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE priority='HOT'  AND date(first_seen)=?",
            (today_str,)
        ).fetchone()[0]
        new_warm = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE priority='WARM' AND date(first_seen)=?",
            (today_str,)
        ).fetchone()[0]
        run_log["new_hot"]  = new_hot
        run_log["new_warm"] = new_warm

        # Record run
        with conn:
            conn.execute(
                """INSERT INTO pipeline_runs
                   (date_range_start, date_range_end, entities_discovered,
                    entities_kept, pdfs_downloaded, enrichment_completed, errors)
                   VALUES (?,?,?,?,?,?,?)""",
                (str(start_date), str(end_date),
                 run_log["entities_discovered"], run_log["entities_kept"],
                 run_log["pdfs_downloaded"],     run_log["enrichment_completed"],
                 json.dumps(run_log["errors"])),
            )
        # ── Notifications ───────────────────────────────────────────────
        _write_notifications(conn, run_log, today_str)
        conn.close()

        _notify(run_log)

    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        _notify_error(str(exc))
    finally:
        _release_lock()


def _auto_start_cadences(conn, cutoff_48h: str, today_str: str) -> None:
    """Start cadence for every HOT lead < 48 h old that has no cadence yet."""
    rows = conn.execute(
        """
        SELECT l.control_number, l.entity_name
        FROM leads l
        LEFT JOIN cadence_tasks ct ON ct.control_number = l.control_number
        WHERE l.priority = 'HOT'
          AND l.first_seen >= ?
          AND ct.id IS NULL
        """,
        (cutoff_48h,),
    ).fetchall()

    if not rows:
        return

    logger.info("Auto-cadence: starting cadence for %d HOT lead(s)", len(rows))
    today = date.today()
    for row in rows:
        cn = row["control_number"]
        try:
            with conn:
                conn.execute("DELETE FROM cadence_tasks WHERE control_number=?", (cn,))
                for touch in _CADENCE:
                    due = today + timedelta(days=touch["day"] - 1)
                    conn.execute(
                        """INSERT INTO cadence_tasks
                           (control_number, step, cadence_day, task_type, label, due_date)
                           VALUES (?,?,?,?,?,?)""",
                        (cn, touch["step"], touch["day"], touch["type"], touch["label"], str(due)),
                    )
                conn.execute(
                    """UPDATE leads SET
                        outreach_status='IN_CADENCE',
                        cadence_start_date=?,
                        cadence_step=1,
                        last_updated=CURRENT_TIMESTAMP
                       WHERE control_number=?""",
                    (today_str, cn),
                )
            logger.info("  Cadence started: %s (%s)", row["entity_name"], cn)
        except Exception as exc:
            logger.error("  Cadence start failed for %s: %s", cn, exc)


def _alert_hot_leads(conn, today_str: str, cutoff_48h: str, mode: str) -> None:
    """Fire macOS notification + optional SMS for each new HOT lead < 48 h old."""
    rows = conn.execute(
        """
        SELECT control_number, entity_name, fit_score, filer_phone, industry_category
        FROM leads
        WHERE priority = 'HOT'
          AND first_seen >= ?
        ORDER BY fit_score DESC
        """,
        (cutoff_48h,),
    ).fetchall()

    if not rows:
        return

    alert_phone = os.environ.get("ALERT_PHONE", "").strip()
    for row in rows:
        name  = row["entity_name"] or row["control_number"]
        score = row["fit_score"] or 0
        cat   = row["industry_category"] or ""
        msg   = f"Score {score} · {cat}" if cat else f"Score {score}"
        _mac_notify(f"HOT lead [{mode}]: {name}", msg)
        if alert_phone:
            sms_body = f"HOT lead: {name} | {msg}"
            _sms_alert(alert_phone, sms_body)


def _sms_alert(phone: str, message: str) -> None:
    """Send SMS via Twilio if credentials are configured. Silent no-op otherwise."""
    sid   = os.environ.get("TWILIO_SID", "").strip()
    token = os.environ.get("TWILIO_TOKEN", "").strip()
    from_ = os.environ.get("TWILIO_FROM", "").strip()
    if not (sid and token and from_):
        return
    try:
        from twilio.rest import Client  # type: ignore
        Client(sid, token).messages.create(to=phone, from_=from_, body=message[:160])
        logger.info("SMS alert sent to %s", phone)
    except ImportError:
        logger.debug("twilio not installed — SMS alert skipped")
    except Exception as exc:
        logger.warning("SMS alert failed: %s", exc)


def _write_notifications(conn, run_log: dict, today_str: str) -> None:
    try:
        from src.notifications import create as notif_create
        # One notification per new HOT lead
        hot_leads = conn.execute(
            "SELECT control_number, entity_name FROM leads "
            "WHERE priority='HOT' AND date(first_seen)=?",
            (today_str,),
        ).fetchall()
        for row in hot_leads:
            notif_create(
                title=f"New HOT lead: {row['entity_name']}",
                body="Discovered by today's pipeline run",
                link=f"/leads/{row['control_number']}",
                ntype="new_lead",
            )
        # Summary notification
        disc = run_log.get("entities_discovered", 0)
        hot  = run_log.get("new_hot", 0)
        warm = run_log.get("new_warm", 0)
        errs = len(run_log.get("errors", []))
        body = f"{disc} scanned · {hot} HOT · {warm} WARM"
        if errs:
            body += f" · {errs} errors"
        notif_create(title="Pipeline complete", body=body, link="/runs", ntype="pipeline_run")
    except Exception as exc:
        logger.warning("Failed to write notifications: %s", exc)


def _notify(run_log: dict) -> None:
    hot  = run_log.get("new_hot", 0)
    warm = run_log.get("new_warm", 0)
    disc = run_log.get("entities_discovered", 0)
    errs = len(run_log.get("errors", []))

    title = "GA Leads Pipeline — Done"
    msg   = (
        f"{disc} entities scanned. "
        f"{hot} HOT  {warm} WARM new leads."
        + (f"  ⚠️ {errs} errors." if errs else "")
    )
    logger.info("Daily run complete: %s", msg)
    _mac_notify(title, msg)


def _notify_error(err: str) -> None:
    _mac_notify("GA Leads Pipeline — ERROR", err[:100])


def _mac_notify(title: str, message: str) -> None:
    try:
        # Sanitize to prevent AppleScript injection via entity names in title/message
        safe_title   = title.replace('"', "'").replace("\\", "")
        safe_message = message.replace('"', "'").replace("\\", "")
        script = (
            f'display notification "{safe_message}" with title "{safe_title}" '
            f'sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass


def _acquire_lock() -> bool:
    # Expire stale locks older than 3 hours
    if LOCK_PATH.exists():
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age > 10_800:
                LOCK_PATH.unlink()
        except OSError:
            pass

    # Atomic create — fails if another process beat us to it
    import errno
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError as e:
        if e.errno == errno.EEXIST:
            return False
        raise


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
