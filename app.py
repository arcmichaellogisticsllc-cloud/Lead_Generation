"""
GA Payment Leads — Sales Pipeline Web App
Run: python app.py
Open: http://localhost:5000
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).parent))
from src.db import DB_PATH, get_connection, init_db

app = Flask(__name__)

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from src.notifications import get_unread, mark_all_read, unread_count


@app.context_processor
def inject_notifications():
    try:
        count = unread_count()
        notifs = get_unread(limit=10) if count else []
    except Exception:
        count, notifs = 0, []
    return {"notif_count": count, "notif_items": notifs}

# ---------------------------------------------------------------------------
# Cadence definition — 12 touches over 12 days
# ---------------------------------------------------------------------------
CADENCE = [
    {"step": 1,  "day": 1,  "type": "call",    "label": "Call"},
    {"step": 2,  "day": 1,  "type": "vm",      "label": "Leave Voicemail"},
    {"step": 3,  "day": 1,  "type": "email",   "label": "Send Intro Email"},
    {"step": 4,  "day": 3,  "type": "call",    "label": "Call"},
    {"step": 5,  "day": 3,  "type": "message", "label": "Short Connect Message"},
    {"step": 6,  "day": 3,  "type": "log",     "label": "Log Day 3 Outcome"},
    {"step": 7,  "day": 6,  "type": "call",    "label": "Call"},
    {"step": 8,  "day": 6,  "type": "email",   "label": "Bump Email"},
    {"step": 9,  "day": 9,  "type": "call",    "label": "Call"},
    {"step": 10, "day": 9,  "type": "email",   "label": "Reference Email"},
    {"step": 11, "day": 12, "type": "call",    "label": "Final Call"},
    {"step": 12, "day": 12, "type": "email",   "label": "Close Loop Email"},
]

CALL_OUTCOMES = ["connected", "no_answer", "vm_left", "wrong_number", "callback_scheduled"]
EMAIL_OUTCOMES = ["sent", "replied", "bounced", "opened"]
FINAL_OUTCOMES = ["converted", "nurture_90", "dead", "no_contact"]

# ---------------------------------------------------------------------------
# Default email templates
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES = {
    3: {
        "name": "Day 1 — Intro",
        "subject": "Quick question for {{business_type}} owner",
        "body": """Hey {{first_name}} —

{{pain_point}}?{{review_hook}}

— Marcus McGee
{{sender_email}}""",
    },
    8: {
        "name": "Day 6 — Bump",
        "subject": "Re: Quick question for {{business_type}} owner",
        "body": """{{outcome_context}}Bumping this up —

Most {{business_type}} businesses in {{city}} are losing 2–3 jobs a month to customers who aren't carrying cash or checks. Is that showing up in your numbers yet?

— Marcus McGee
{{sender_email}}""",
    },
    10: {
        "name": "Day 9 — Proof Point",
        "subject": "What a {{business_type}} in {{nearby_city}} did",
        "body": """{{outcome_context}}Hey {{first_name}} —

{{stat}}

Worth a 10-minute call this week?

— Marcus McGee
{{sender_email}}""",
    },
    12: {
        "name": "Day 12 — Close Loop",
        "subject": "Closing the loop on {{business_name}}",
        "body": """Hey {{first_name}} —

{{outcome_context}}I'm not going to keep pinging you — but I do want to leave you with one question:

If every job paid on the spot, what would that change about how you run your week?

Reach out whenever it's the right time.

— Marcus McGee
{{sender_email}}""",
    },
    2: {
        "name": "VM Script",
        "subject": "",
        "body": """"Hey {{first_name}}, Marcus McGee here from IPPayware — quick question for you, I'll make it worth a 30-second callback. {{sender_email}}. Again, {{sender_email}}." """,
    },
}

SENDER_EMAIL = "mmcgee@ippayware.com"

# Industry-matched proof points — keyed by normalized industry slug
PROOF_POINTS: dict[str, str] = {
    "plumbing":    "A plumber in Gwinnett County added $1,400/month just by giving customers a way to pay on the spot.",
    "hvac":        "An HVAC tech in Alpharetta was losing 1–2 jobs a month to 'I don't have my checkbook.' Not anymore.",
    "landscap":    "A landscaper in Marietta was spending 2 hours every Friday chasing invoices. Now he's paid before he loads the truck.",
    "clean":       "A cleaning crew in Buckhead lost 3 clients last year because they only took checks. Fixed it in a weekend.",
    "electrical":  "An electrician in Decatur cut his average collection time from 18 days to same-day. Same jobs, same customers.",
    "construct":   "A contractor in Cobb County was writing off $800–$1,200 in unpaid invoices every month. That number is now zero.",
    "contract":    "A contractor in Cobb County was writing off $800–$1,200 in unpaid invoices every month. That number is now zero.",
    "paint":       "A painting company in Roswell stopped chasing checks the week they gave customers a link to pay from their phone.",
    "roofing":     "A roofing crew in Cherokee County collected $11K in same-day payments the first month they tried it.",
    "pest":        "A pest control company in Forsyth County increased same-day collection by 40% without changing a single service.",
    "pool":        "A pool service business in Fulton County cut their 30-day outstanding balance in half in one billing cycle.",
    "default":     "A home service business in Metro Atlanta went from 60% same-day payment to 94% — without changing a single thing about their service.",
}

# Industry-specific opening questions
INDUSTRY_PAIN_POINTS: dict[str, str] = {
    "plumbing":    'How many jobs last month ended with "I\'ll mail you a check"',
    "hvac":        "What percentage of your HVAC calls end with a promise to pay later",
    "landscap":    "How many hours a week do you spend chasing invoice payments",
    "clean":       "How many clients are still \"getting to it\" on last month's invoice",
    "electrical":  "How many customers still owe you from work done 30+ days ago",
    "construct":   "How much are you carrying in outstanding invoices right now",
    "contract":    "How much are you carrying in outstanding invoices right now",
    "paint":       "How many customers still owe you from last month's jobs",
    "roofing":     "How many jobs last quarter ended with a payment plan that never got set up",
    "pest":        "How many service calls end with \"just send me an invoice\"",
    "pool":        "How many pool service clients are 30+ days behind right now",
    "default":     'How many jobs last month ended with "I\'ll mail you a check"',
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db():
    return get_connection(DB_PATH)


def _seed_templates(conn):
    for step, t in DEFAULT_TEMPLATES.items():
        exists = conn.execute(
            "SELECT 1 FROM email_templates WHERE step=?", (step,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO email_templates (step, name, subject, body) VALUES (?,?,?,?)",
                (step, t["name"], t["subject"], t["body"]),
            )
    conn.commit()


def _get_templates(conn) -> dict:
    rows = conn.execute("SELECT * FROM email_templates").fetchall()
    return {r["step"]: dict(r) for r in rows}


def _industry_key(industry_category: str) -> str:
    cat = (industry_category or "").lower()
    for key in ["plumbing", "hvac", "landscap", "clean", "electrical",
                "construct", "contract", "paint", "roofing", "pest", "pool", "food", "retail"]:
        if key in cat:
            return key
    return "default"


def _outcome_context(log_history: list[dict]) -> str:
    """Adaptive opener based on prior outreach outcomes — appended to template start."""
    if not log_history:
        return ""
    for entry in log_history:
        if entry.get("task_type") == "email" and entry.get("outcome") == "replied":
            return "Thanks for getting back to me — "
    for entry in log_history:
        if entry.get("task_type") == "call" and entry.get("outcome") == "connected":
            return "Good talking with you — wanted to follow up. "
    vm_count = sum(
        1 for e in log_history
        if e.get("task_type") in ("call", "vm") and e.get("outcome") in ("vm_left", "no_answer")
    )
    if vm_count >= 2:
        return "I know I've tried reaching you a couple times — "
    return ""


def _review_hook(lead: dict) -> str:
    """Returns a subtle hook line if review signals were found, else empty string."""
    snippet = lead.get("review_snippet") or ""
    if not snippet:
        return ""
    if lead.get("review_has_payment_friction"):
        return "\n\nI came across some feedback about your business online — payment friction seems to come up. Wanted to reach out."
    return "\n\nI came across your business online — looks like you're building a reputation in the area."


def _render_template_body(body: str, lead: dict, log_history: list[dict] | None = None) -> str:
    ikey          = _industry_key(lead.get("industry_category") or "")
    first_name    = (lead.get("organizer_name") or "there").split()[0].title()
    business_type = (lead.get("industry_category") or "home service").lower()
    city          = _city_from_address(lead.get("principal_office_address") or "")
    nearby        = _nearby_city(city)
    stat          = PROOF_POINTS.get(ikey) or PROOF_POINTS["default"]
    pain_point    = INDUSTRY_PAIN_POINTS.get(ikey) or INDUSTRY_PAIN_POINTS["default"]
    outcome_ctx   = _outcome_context(log_history or [])
    review_hook   = _review_hook(lead)

    return (
        body
        .replace("{{first_name}}",      first_name)
        .replace("{{business_name}}",   lead.get("entity_name") or "")
        .replace("{{business_type}}",   business_type)
        .replace("{{city}}",            city or "your area")
        .replace("{{nearby_city}}",     nearby)
        .replace("{{stat}}",            stat)
        .replace("{{pain_point}}",      pain_point)
        .replace("{{outcome_context}}", outcome_ctx)
        .replace("{{review_hook}}",     review_hook)
        .replace("{{sender_email}}",    SENDER_EMAIL)
    )


def _make_mailto(to_email: str, subject: str, body: str) -> str:
    from urllib.parse import quote
    if not to_email:
        return ""
    return (
        f"mailto:{to_email}"
        f"?subject={quote(subject, safe='')}"
        f"&body={quote(body, safe='')}"
    )


def _city_from_address(addr: str) -> str:
    m = re.search(r",\s*([A-Za-z ]+),\s*GA\b", addr)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b([A-Za-z ]{3,20})\s+GA\b", addr)
    if m:
        return m.group(1).strip()
    return ""


def _nearby_city(city: str) -> str:
    ga_cities = ["Atlanta", "Marietta", "Alpharetta", "Gwinnett County",
                 "Cobb County", "Decatur", "Roswell", "Kennesaw"]
    if city and city.title() in ga_cities:
        others = [c for c in ga_cities if c != city.title()]
        return others[0]
    return "Metro Atlanta"


def _cadence_status(conn, control_number: str) -> dict:
    tasks = conn.execute(
        "SELECT * FROM cadence_tasks WHERE control_number=? ORDER BY step",
        (control_number,),
    ).fetchall()
    if not tasks:
        return {"active": False, "tasks": [], "next_task": None, "completed": 0}
    completed  = sum(1 for t in tasks if t["status"] == "done")
    next_task  = next((t for t in tasks if t["status"] == "pending"), None)
    return {
        "active":     True,
        "tasks":      [dict(t) for t in tasks],
        "next_task":  dict(next_task) if next_task else None,
        "completed":  completed,
        "total":      len(tasks),
    }


def _today_tasks(conn) -> list[dict]:
    today = str(date.today())
    rows = conn.execute(
        """
        SELECT ct.*, l.entity_name, l.organizer_name, l.filer_phone,
               l.principal_office_address, l.industry_category, l.priority
        FROM cadence_tasks ct
        JOIN leads l ON ct.control_number = l.control_number
        WHERE ct.status = 'pending' AND ct.due_date <= ?
        ORDER BY l.fit_score DESC, ct.due_date, ct.step
        """,
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    conn = db()
    _seed_templates(conn)
    tasks    = _today_tasks(conn)
    overdue  = [t for t in tasks if t["due_date"] < str(date.today())]
    due_today = [t for t in tasks if t["due_date"] == str(date.today())]

    stats = {
        "total_leads": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE priority != 'SKIP'"
        ).fetchone()[0],
        "in_cadence": conn.execute(
            "SELECT COUNT(DISTINCT control_number) FROM cadence_tasks WHERE status='pending'"
        ).fetchone()[0],
        "hot": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE priority='HOT'"
        ).fetchone()[0],
        "warm": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE priority='WARM'"
        ).fetchone()[0],
        "converted": conn.execute(
            "SELECT COUNT(*) FROM leads WHERE outreach_status='CONVERTED'"
        ).fetchone()[0],
    }

    # New leads added today by the daily pipeline (HOT/WARM only, no cadence yet)
    today_str = str(date.today())
    new_leads_rows = conn.execute(
        """
        SELECT l.*
        FROM leads l
        LEFT JOIN cadence_tasks ct ON ct.control_number = l.control_number
        WHERE l.priority IN ('HOT', 'WARM')
          AND date(l.first_seen) = ?
          AND ct.id IS NULL
        ORDER BY l.fit_score DESC
        """,
        (today_str,),
    ).fetchall()
    new_leads = [dict(r) for r in new_leads_rows]

    conn.close()
    return render_template(
        "dashboard.html",
        overdue=overdue,
        due_today=due_today,
        stats=stats,
        new_leads=new_leads,
        today=date.today().strftime("%A, %B %d"),
    )


# ---------------------------------------------------------------------------
# Routes — Leads
# ---------------------------------------------------------------------------

@app.route("/leads")
def leads():
    conn  = db()
    status_filter   = request.args.get("status", "active")
    priority_filter = request.args.get("priority", "ALL")

    sql = "SELECT * FROM leads WHERE priority != 'SKIP'"
    if priority_filter != "ALL":
        sql += f" AND priority = '{priority_filter}'"
    if status_filter == "active":
        sql += " AND outreach_status NOT IN ('CONVERTED','DEAD')"
    elif status_filter == "closed":
        sql += " AND outreach_status IN ('CONVERTED','DEAD','NURTURE')"
    sql += " ORDER BY fit_score DESC"

    rows = [dict(r) for r in conn.execute(sql).fetchall()]

    # Attach cadence progress to each lead
    for lead in rows:
        cs = _cadence_status(conn, lead["control_number"])
        lead["_cadence"] = cs

    conn.close()
    return render_template(
        "leads.html",
        leads=rows,
        status_filter=status_filter,
        priority_filter=priority_filter,
    )


@app.route("/leads/<cn>")
def lead_detail(cn: str):
    conn = db()
    _seed_templates(conn)
    lead = conn.execute(
        "SELECT * FROM leads WHERE control_number=?", (cn,)
    ).fetchone()
    if not lead:
        conn.close()
        return "Lead not found", 404

    lead = dict(lead)
    cs   = _cadence_status(conn, cn)
    log  = conn.execute(
        "SELECT * FROM outreach_log WHERE control_number=? ORDER BY logged_at DESC",
        (cn,),
    ).fetchall()
    log_list  = [dict(r) for r in log]
    templates = _get_templates(conn)

    # Build rendered email drafts — pass log history for adaptive copy
    rendered = {}
    for step, tmpl in templates.items():
        subj = _render_template_body(tmpl["subject"], lead, log_list)
        body = _render_template_body(tmpl["body"],    lead, log_list)
        rendered[step] = {
            "subject":  subj,
            "body":     body,
            "mailto":   _make_mailto(lead.get("filer_email") or "", subj, body),
        }

    conn.close()
    return render_template(
        "lead_detail.html",
        lead=lead,
        cadence=cs,
        cadence_def=CADENCE,
        log=log_list,
        call_outcomes=CALL_OUTCOMES,
        email_outcomes=EMAIL_OUTCOMES,
        final_outcomes=FINAL_OUTCOMES,
        rendered=rendered,
        sender_email=SENDER_EMAIL,
        today=str(date.today()),
    )


# ---------------------------------------------------------------------------
# Routes — Cadence actions
# ---------------------------------------------------------------------------

@app.route("/leads/<cn>/start", methods=["POST"])
def start_cadence(cn: str):
    conn  = db()
    today = date.today()

    # Clear any previous tasks
    conn.execute("DELETE FROM cadence_tasks WHERE control_number=?", (cn,))

    for touch in CADENCE:
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
        (str(today), cn),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("lead_detail", cn=cn))


@app.route("/leads/<cn>/complete_task", methods=["POST"])
def complete_task(cn: str):
    task_id = request.form.get("task_id")
    outcome = request.form.get("outcome", "")
    notes   = request.form.get("notes", "")

    conn = db()
    task = conn.execute(
        "SELECT * FROM cadence_tasks WHERE id=? AND control_number=?",
        (task_id, cn),
    ).fetchone()

    if task:
        conn.execute(
            """UPDATE cadence_tasks
               SET status='done', completed_at=CURRENT_TIMESTAMP,
                   outcome=?, notes=?
               WHERE id=?""",
            (outcome, notes, task_id),
        )
        conn.execute(
            """INSERT INTO outreach_log
               (control_number, step, cadence_day, task_type, outcome, notes)
               VALUES (?,?,?,?,?,?)""",
            (cn, task["step"], task["cadence_day"], task["task_type"], outcome, notes),
        )
        # Advance cadence_step
        conn.execute(
            "UPDATE leads SET cadence_step=?, last_updated=CURRENT_TIMESTAMP WHERE control_number=?",
            (task["step"] + 1, cn),
        )
    conn.commit()
    conn.close()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("lead_detail", cn=cn))


@app.route("/leads/<cn>/outcome", methods=["POST"])
def set_outcome(cn: str):
    outcome = request.form.get("outcome")
    notes   = request.form.get("notes", "")

    status_map = {
        "converted":  "CONVERTED",
        "nurture_90": "NURTURE",
        "dead":       "DEAD",
        "no_contact": "NO_CONTACT",
    }
    db_status = status_map.get(outcome, outcome.upper())

    conn = db()
    conn.execute(
        "UPDATE cadence_tasks SET status='skipped' WHERE control_number=? AND status='pending'",
        (cn,),
    )
    conn.execute(
        """UPDATE leads SET outreach_status=?, notes=?,
           last_updated=CURRENT_TIMESTAMP WHERE control_number=?""",
        (db_status, notes, cn),
    )
    conn.execute(
        """INSERT INTO outreach_log (control_number, task_type, outcome, notes)
           VALUES (?, 'outcome', ?, ?)""",
        (cn, outcome, notes),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("lead_detail", cn=cn))


# ---------------------------------------------------------------------------
# Routes — Templates
# ---------------------------------------------------------------------------

@app.route("/templates")
def email_templates():
    conn      = db()
    _seed_templates(conn)
    templates = _get_templates(conn)
    conn.close()
    return render_template(
        "templates_edit.html",
        templates=templates,
        cadence_def=CADENCE,
        proof_points=PROOF_POINTS,
    )


@app.route("/templates/save", methods=["POST"])
def save_template():
    step    = int(request.form["step"])
    subject = request.form.get("subject", "")
    body    = request.form["body"]

    conn = db()
    conn.execute(
        """INSERT INTO email_templates (step, name, subject, body, updated_at)
           VALUES (?, (SELECT name FROM email_templates WHERE step=?), ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(step) DO UPDATE SET subject=excluded.subject,
               body=excluded.body, updated_at=CURRENT_TIMESTAMP""",
        (step, step, subject, body),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("email_templates") + f"#step-{step}")


# ---------------------------------------------------------------------------
# Routes — Enrich (manual trigger)
# ---------------------------------------------------------------------------

@app.route("/leads/<cn>/enrich", methods=["POST"])
def enrich_lead_route(cn: str):
    """Run full enrichment (website + payment stack + social profiles) for one lead."""
    from src.enrich import enrich_lead as _enrich
    from patchright.sync_api import sync_playwright
    from src.discover import USER_AGENT

    conn = db()
    row  = conn.execute("SELECT * FROM leads WHERE control_number=?", (cn,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("leads"))

    lead = dict(row)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--window-position=3000,3000"])
        ctx  = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            updated = _enrich(lead, browser_page=page, find_social=True)
        finally:
            browser.close()

    conn.execute(
        """UPDATE leads SET
            website=?, has_website=?, has_online_payment=?,
            has_online_booking=?, detected_payment_processor=?,
            detected_vertical_saas=?, invoice_workflow_signals=?,
            facebook_url=?, instagram_url=?, linkedin_url=?,
            yelp_url=?, angi_url=?, google_maps_url=?,
            profiles_searched=?,
            review_snippet=?, review_source=?,
            review_has_payment_friction=?,
            fit_score=?, score_breakdown=?, priority=?,
            last_updated=CURRENT_TIMESTAMP
           WHERE control_number=?""",
        (
            updated.get("website"),
            int(updated.get("has_website", False)),
            int(updated.get("has_online_payment", False)),
            int(updated.get("has_online_booking", False)),
            updated.get("detected_payment_processor"),
            updated.get("detected_vertical_saas"),
            int(updated.get("invoice_workflow_signals", False)),
            updated.get("facebook_url"),
            updated.get("instagram_url"),
            updated.get("linkedin_url"),
            updated.get("yelp_url"),
            updated.get("angi_url"),
            updated.get("google_maps_url"),
            1,
            updated.get("review_snippet"),
            updated.get("review_source"),
            int(updated.get("review_has_payment_friction", 0)),
            updated.get("fit_score"),
            updated.get("score_breakdown"),
            updated.get("priority"),
            cn,
        ),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("lead_detail", cn=cn))


# ---------------------------------------------------------------------------
# Routes — Kanban pipeline view
# ---------------------------------------------------------------------------

STAGES_DEF = [
    {"key": "new",    "label": "New",     "sublabel": "No cadence",  "color": "#6c757d"},
    {"key": "day1",   "label": "Day 1",   "sublabel": "Steps 1–3",   "color": "#0d6efd"},
    {"key": "day3",   "label": "Day 3",   "sublabel": "Steps 4–6",   "color": "#6f42c1"},
    {"key": "day6",   "label": "Day 6",   "sublabel": "Steps 7–8",   "color": "#fd7e14"},
    {"key": "day9",   "label": "Day 9",   "sublabel": "Steps 9–10",  "color": "#ffc107"},
    {"key": "day12",  "label": "Day 12",  "sublabel": "Steps 11–12", "color": "#dc3545"},
    {"key": "closed", "label": "Closed",  "sublabel": "",            "color": "#198754"},
]

_CLOSED_STATUSES = {"CONVERTED", "DEAD", "NURTURE", "NO_CONTACT"}

def _cadence_stage(next_step: int | None, outreach_status: str | None) -> str:
    if outreach_status in _CLOSED_STATUSES:
        return "closed"
    if next_step is None:
        return "new"
    if next_step <= 3:
        return "day1"
    if next_step <= 6:
        return "day3"
    if next_step <= 8:
        return "day6"
    if next_step <= 10:
        return "day9"
    return "day12"


@app.route("/pipeline")
def pipeline_kanban():
    conn  = db()
    today = str(date.today())
    stale_cutoff = str(date.today() - timedelta(days=3))

    rows = conn.execute(
        """
        SELECT l.*,
               (SELECT MIN(ct.step)     FROM cadence_tasks ct
                WHERE ct.control_number = l.control_number AND ct.status='pending') AS _next_step,
               (SELECT ct.label         FROM cadence_tasks ct
                WHERE ct.control_number = l.control_number AND ct.status='pending'
                ORDER BY ct.step LIMIT 1) AS _next_task_label,
               (SELECT ct.task_type     FROM cadence_tasks ct
                WHERE ct.control_number = l.control_number AND ct.status='pending'
                ORDER BY ct.step LIMIT 1) AS _next_task_type,
               (SELECT MIN(ct.due_date) FROM cadence_tasks ct
                WHERE ct.control_number = l.control_number AND ct.status='pending') AS _next_due,
               (SELECT MAX(ol.logged_at) FROM outreach_log ol
                WHERE ol.control_number = l.control_number) AS _last_activity
        FROM leads l
        WHERE l.priority != 'SKIP'
        ORDER BY l.fit_score DESC
        """
    ).fetchall()
    conn.close()

    stages: dict[str, list] = {s["key"]: [] for s in STAGES_DEF}

    for row in rows:
        lead = dict(row)
        stage = _cadence_stage(lead.get("_next_step"), lead.get("outreach_status"))
        next_due      = lead.get("_next_due")
        last_activity = lead.get("_last_activity")
        lead["_overdue"] = bool(next_due and next_due < today)
        is_active = stage not in ("new", "closed")
        if is_active and last_activity:
            lead["_stale"] = last_activity[:10] <= stale_cutoff and not lead["_overdue"]
        elif is_active and not last_activity:
            # cadence started but nothing logged — check if cadence is old
            lead["_stale"] = False
        else:
            lead["_stale"] = False
        stages[stage].append(lead)

    return render_template(
        "pipeline_kanban.html",
        stages=stages,
        stages_def=STAGES_DEF,
        today=today,
    )


# ---------------------------------------------------------------------------
# Routes — Notifications
# ---------------------------------------------------------------------------

@app.route("/notifications/read", methods=["POST"])
def notifications_read():
    mark_all_read()
    return redirect(request.referrer or "/")


# ---------------------------------------------------------------------------
# Routes — Pipeline run history
# ---------------------------------------------------------------------------

@app.route("/runs")
def pipeline_runs():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM pipeline_runs ORDER BY run_date DESC LIMIT 60"
    ).fetchall()
    conn.close()

    runs = []
    for r in rows:
        d = dict(r)
        try:
            import json as _json
            d["errors_list"] = _json.loads(d.get("errors") or "[]")
        except Exception:
            d["errors_list"] = []
        runs.append(d)

    return render_template("runs.html", runs=runs)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    conn = db()
    _seed_templates(conn)
    conn.close()
    print("\n  GA Payment Leads — Sales Pipeline")
    print("  Open: http://localhost:5000\n")
    app.run(debug=True, port=5000)
