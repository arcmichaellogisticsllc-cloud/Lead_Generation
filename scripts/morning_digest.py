"""
Morning digest — runs at 7:30 AM weekdays (after daily_pipeline.py at 7:00 AM).

Checks:
  1. Tasks due today (by type — calls, emails, VMs)
  2. Overdue tasks (due before today, still pending)
  3. Stale leads (cadence active, no activity for STALE_DAYS)

Creates in-app notifications for each stale/overdue lead.
Sends one macOS push notification with the summary.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

STALE_DAYS = 3   # days without activity before a lead is flagged


def main() -> None:
    from src.db import DB_PATH, get_connection
    from src.notifications import create as notif_create

    conn = get_connection(DB_PATH)
    today = str(date.today())
    stale_cutoff = str(date.today() - timedelta(days=STALE_DAYS))

    # ── Tasks due today ────────────────────────────────────────────────────
    due_rows = conn.execute(
        "SELECT task_type, COUNT(*) as n FROM cadence_tasks "
        "WHERE status='pending' AND due_date=? GROUP BY task_type",
        (today,),
    ).fetchall()
    due_by_type = {r["task_type"]: r["n"] for r in due_rows}
    total_due   = sum(due_by_type.values())

    # ── Overdue tasks ─────────────────────────────────────────────────────
    overdue_count = conn.execute(
        "SELECT COUNT(DISTINCT control_number) FROM cadence_tasks "
        "WHERE status='pending' AND due_date < ?",
        (today,),
    ).fetchone()[0]

    if overdue_count:
        notif_create(
            title=f"{overdue_count} overdue task{'s' if overdue_count != 1 else ''}",
            body="Touches that should have happened before today",
            link="/",
            ntype="overdue_task",
        )

    # ── Stale leads ───────────────────────────────────────────────────────
    stale_rows = conn.execute(
        """
        SELECT l.control_number, l.entity_name,
               MAX(ol.logged_at) as last_activity
        FROM leads l
        JOIN cadence_tasks ct ON ct.control_number = l.control_number
        LEFT JOIN outreach_log ol ON ol.control_number = l.control_number
        WHERE ct.status = 'pending'
          AND l.outreach_status NOT IN ('CONVERTED','DEAD','NURTURE','NO_CONTACT')
        GROUP BY l.control_number
        HAVING last_activity IS NULL OR date(last_activity) <= ?
        ORDER BY last_activity ASC
        """,
        (stale_cutoff,),
    ).fetchall()

    for row in stale_rows:
        last = row["last_activity"]
        if last:
            days_ago = (date.today() - datetime.fromisoformat(last[:10]).date()).days
            body = f"No activity for {days_ago} days"
        else:
            body = "Cadence started — no activity logged yet"
        notif_create(
            title=f"Stale: {row['entity_name']}",
            body=body,
            link=f"/leads/{row['control_number']}",
            ntype="stale_lead",
        )

    conn.close()

    # ── macOS push notification ───────────────────────────────────────────
    parts = []
    if total_due:
        call_n  = due_by_type.get("call", 0) + due_by_type.get("vm", 0)
        email_n = due_by_type.get("email", 0)
        parts.append(f"{total_due} tasks due")
        if call_n:
            parts.append(f"{call_n} calls")
        if email_n:
            parts.append(f"{email_n} emails")
    if overdue_count:
        parts.append(f"{overdue_count} overdue")
    if stale_rows:
        parts.append(f"{len(stale_rows)} stale lead{'s' if len(stale_rows) != 1 else ''}")

    if not parts:
        msg = "Nothing due today — pipeline is clear."
    else:
        msg = " · ".join(parts)

    _mac_notify("GA Leads — Morning Digest", msg)
    print(f"Digest: {msg}")


def _mac_notify(title: str, message: str) -> None:
    try:
        script = (
            f'display notification "{message}" with title "{title}" '
            f'sound name "default"'
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    main()
