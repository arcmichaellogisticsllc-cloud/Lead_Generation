"""
GA Payment Leads — Sales Pipeline Web App
Run: python app.py
Open: http://localhost:5000
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).parent))
from src.cadence import CADENCE, CALL_OUTCOMES, EMAIL_OUTCOMES, FINAL_OUTCOMES
from src.db import DB_PATH, get_connection, init_db
from src.notifications import get_unread, mark_all_read, unread_count

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


@app.context_processor
def inject_notifications():
    try:
        count = unread_count()
        notifs = get_unread(limit=10) if count else []
    except Exception:
        count, notifs = 0, []
    return {"notif_count": count, "notif_items": notifs}


@app.template_filter("phone_fmt")
def phone_fmt(value: str) -> str:
    """Format a phone number as (NXX) NXX-XXXX."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return value


@app.template_filter("time_fmt")
def time_fmt(value: str) -> str:
    """Format a timestamp string as '10:34 AM'."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%-I:%M %p")
    except Exception:
        return str(value)[:5]


# CADENCE, CALL_OUTCOMES, EMAIL_OUTCOMES, FINAL_OUTCOMES imported from src.cadence above

# ---------------------------------------------------------------------------
# Default email templates
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES = {
    3: {
        "name": "Day 1 — Intro",
        "subject": "Quick question for {{business_type}} owner",
        "body": """Hey {{first_name}} —

{{web_presence}} — {{pain_point}}?{{review_detail}}

— Marcus McGee
{{sender_email}}""",
    },
    8: {
        "name": "Day 6 — Bump",
        "subject": "Re: Quick question for {{business_type}} owner",
        "body": """{{outcome_context}}Bumping this up —{{stack_hook}}

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
    5: {
        "name": "Day 3 — Text",
        "subject": "",
        "body": """Hey {{first_name}} — Marcus from IPPayware. {{pain_point}}? Worth a quick chat — reply or call me back. — Marcus""",
    },
}

SENDER_EMAIL   = os.environ.get("SENDER_EMAIL",   "mmcgee@ippayware.com")
SENDER_NAME    = os.environ.get("SENDER_NAME",    "Marcus McGee")
SENDER_PHONE   = os.environ.get("SENDER_PHONE",   "")
SENDER_COMPANY = os.environ.get("SENDER_COMPANY", "IPPayware")

# ---------------------------------------------------------------------------
# Processor / SaaS — specific acknowledgment copy for the bump email.
# These references position IPPayware as complementary, not competitive.
# ---------------------------------------------------------------------------
_PROCESSOR_COPY: dict[str, str] = {
    "square":   "I see you're set up with Square — most field service businesses use it for in-person, but the gap is job-site collection before the crew leaves.",
    "stripe":   "I see you're using Stripe — great for online, but collecting at the job site is still a separate problem for most service businesses I work with.",
    "toast":    "I see you're on Toast — that covers the counter, but what do customers do when they want to pay remotely or split a job invoice?",
    "clover":   "I see you're using Clover — solid for counter payments, but what about field collection and on-site invoicing before the truck leaves?",
    "paypal":   'I see you\'re accepting PayPal — most customers who need to "send a PayPal" end up delaying a day or two. That adds up.',
}

_SAAS_COPY: dict[str, str] = {
    "servicetitan":  "I see you're running ServiceTitan — great for dispatch, but most techs I talk to are still collecting cash or waiting on checks at job close.",
    "housecall_pro": "I see you're using Housecall Pro — solid for scheduling, but what's your collection rate before the truck leaves the driveway?",
    "jobber":        "I see you're using Jobber — most Jobber shops I talk to still have a 20–30% 'pay later' rate at job close.",
    "mindbody":      "I noticed you're on Mindbody — handles memberships well, but what about same-day collection for one-off services?",
    "vagaro":        "I see you're using Vagaro — solid for booking, but what happens when a client wants to pay and their card isn't on file?",
    "booker":        "I see you're on Booker — works well for recurring clients, but what about walk-ins and field estimates?",
}

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


def _web_presence(lead: dict) -> str:
    """Reference to where we found this business — anchors the cold reach."""
    if lead.get("website"):
        try:
            from urllib.parse import urlparse
            domain = re.sub(r"^www\.", "", urlparse(lead["website"]).netloc)
            return f"I came across your site ({domain})"
        except Exception:
            return "I came across your site"
    if lead.get("yelp_url"):
        return "I came across your Yelp listing"
    if lead.get("google_maps_url"):
        return "I found your Google listing"
    if lead.get("angi_url"):
        return "I found your Angi profile"
    return "I came across your business"


def _stack_hook(lead: dict) -> str:
    """Processor/SaaS acknowledgment + gap pivot, or website-no-payment signal.

    Returns a paragraph prefixed with \\n\\n when a signal is present, or ''
    so the surrounding template whitespace collapses cleanly when empty.
    """
    processor = (lead.get("detected_payment_processor") or "").lower()
    saas      = (lead.get("detected_vertical_saas") or "").lower()
    has_web   = bool(lead.get("has_website"))
    has_pay   = bool(lead.get("has_online_payment"))
    invoice   = bool(lead.get("invoice_workflow_signals"))

    if processor in _PROCESSOR_COPY:
        return "\n\n" + _PROCESSOR_COPY[processor]
    if saas in _SAAS_COPY:
        return "\n\n" + _SAAS_COPY[saas]
    if has_web and not has_pay:
        return "\n\nI visited your site — no way for customers to pay online. That's the most common gap I help fix."
    if invoice and not has_pay:
        return "\n\nI noticed your site mentions invoicing — the gap I see most is invoice-to-collection lag: jobs getting paid 10–30 days after they close."
    return ""


def _review_detail(lead: dict) -> str:
    """More specific review reference using the actual snippet when friction is found."""
    snippet = lead.get("review_snippet") or ""
    if not snippet or not lead.get("review_has_payment_friction"):
        return ""
    kw_list = ["cash only", "cash", "check", "credit card", "card", "payment",
               "invoice", "venmo", "zelle", "bill"]
    best = ""
    for kw in kw_list:
        idx = snippet.lower().find(kw)
        if idx >= 0:
            start = max(0, idx - 25)
            end   = min(len(snippet), idx + 80)
            frag  = snippet[start:end].strip(' .,!"')
            if not best or len(frag) < len(best):
                best = frag
    if best and len(best) > 10:
        return f'\n\nI saw a review that mentioned: "{best}…" — that\'s exactly the gap I help close.'
    return "\n\nI noticed some reviews mention payment friction. That's exactly what I help with."


def _sanitize_template_value(v: str) -> str:
    """Strip control characters that could inject headers into mailto: body."""
    return v.replace("\r", "").replace("\n", " ").replace("\0", "")


def _render_template_body(body: str, lead: dict, log_history: list[dict] | None = None) -> str:
    ikey          = _industry_key(lead.get("industry_category") or "")
    first_name    = _sanitize_template_value(
        (lead.get("organizer_name") or "there").split()[0].title()
    )
    business_type = _sanitize_template_value(
        (lead.get("industry_category") or "home service").lower()
    )
    city          = _sanitize_template_value(
        _city_from_address(lead.get("principal_office_address") or "")
    )
    nearby        = _sanitize_template_value(_nearby_city(city))
    stat          = PROOF_POINTS.get(ikey) or PROOF_POINTS["default"]
    pain_point    = INDUSTRY_PAIN_POINTS.get(ikey) or INDUSTRY_PAIN_POINTS["default"]
    outcome_ctx   = _outcome_context(log_history or [])
    review_hook   = _review_hook(lead)
    entity_name   = _sanitize_template_value(lead.get("entity_name") or "")

    # Phase 5 — context-aware personalization signals
    web_presence  = _web_presence(lead)
    stack_hook    = _stack_hook(lead)
    review_detail = _review_detail(lead)

    return (
        body
        .replace("{{first_name}}",      first_name)
        .replace("{{business_name}}",   entity_name)
        .replace("{{business_type}}",   business_type)
        .replace("{{city}}",            city or "your area")
        .replace("{{nearby_city}}",     nearby)
        .replace("{{stat}}",            stat)
        .replace("{{pain_point}}",      pain_point)
        .replace("{{outcome_context}}", outcome_ctx)
        .replace("{{review_hook}}",     review_hook)
        .replace("{{web_presence}}",    web_presence)
        .replace("{{stack_hook}}",      stack_hook)
        .replace("{{review_detail}}",   review_detail)
        .replace("{{sender_email}}",    SENDER_EMAIL)
        .replace("{{sender_name}}",     SENDER_NAME)
        .replace("{{sender_phone}}",    SENDER_PHONE)
        .replace("{{sender_company}}",  SENDER_COMPANY)
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


def _make_sms(to_phone: str, body: str) -> str:
    from urllib.parse import quote
    if not to_phone:
        return ""
    return f"sms:{to_phone}?body={quote(body, safe='')}"


def _best_phone(lead: dict) -> str | None:
    """Return the best available outreach phone, preferring filing data over scraped."""
    return (
        lead.get("filer_phone")
        or lead.get("business_phone")
        or lead.get("owner_personal_phone")
        or None
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
        SELECT ct.*, l.entity_name, l.organizer_name,
               l.filer_phone, l.business_phone, l.filer_email,
               l.principal_office_address, l.industry_category, l.priority
        FROM cadence_tasks ct
        JOIN leads l ON ct.control_number = l.control_number
        WHERE ct.status = 'pending' AND ct.due_date <= ?
        ORDER BY (l.fit_score * 1.0 / MAX(1, CAST(julianday('now') - julianday(l.formation_date) AS INTEGER))) DESC,
                 ct.due_date, ct.step
        """,
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    # Optional date param for catching up (defaults to today)
    date_param = request.args.get("date", "")
    try:
        selected = date.fromisoformat(date_param)
    except ValueError:
        selected = date.today()
    selected_str  = str(selected)
    today_str     = str(date.today())

    active_filter = request.args.get("filter", "")
    if active_filter not in ("overdue", "new", ""):
        active_filter = ""

    conn = db()
    _seed_templates(conn)

    # ── Queue: pending tasks due on or before selected date ──────────
    rows = conn.execute(
        """
        SELECT ct.id AS task_id, ct.control_number, ct.step, ct.cadence_day,
               ct.task_type, ct.label AS action_label, ct.due_date,
               l.entity_name, l.industry_category, l.priority, l.fit_score,
               l.filer_phone, l.business_phone, l.owner_personal_phone,
               l.filer_email, l.principal_office_address, l.organizer_name
        FROM cadence_tasks ct
        JOIN leads l ON ct.control_number = l.control_number
        WHERE ct.status = 'pending' AND ct.due_date <= ?
        ORDER BY ct.due_date ASC, l.fit_score DESC, ct.step
        """,
        (selected_str,),
    ).fetchall()

    all_tasks = []
    for r in rows:
        t = dict(r)
        t["is_overdue"]  = t["due_date"] < today_str
        t["total_steps"] = 12
        t["phone"]       = _best_phone(t)
        t["score"]       = t.pop("fit_score", 0) or 0
        all_tasks.append(t)

    overdue_count   = sum(1 for t in all_tasks if t["is_overdue"])
    due_today_count = sum(1 for t in all_tasks if t["due_date"] == today_str)

    if active_filter == "overdue":
        queue = [t for t in all_tasks if t["is_overdue"]]
    else:
        queue = all_tasks

    # ── New leads: HOT/WARM with no cadence started ──────────────────
    new_rows = conn.execute(
        """
        SELECT l.control_number, l.entity_name, l.industry_category,
               l.formation_date, l.fit_score, l.priority,
               l.filer_phone, l.business_phone, l.owner_personal_phone,
               l.filer_email, l.organizer_name, l.principal_office_address
        FROM leads l
        LEFT JOIN cadence_tasks ct ON ct.control_number = l.control_number
        WHERE l.priority IN ('HOT', 'WARM')
          AND (l.outreach_status IS NULL
               OR l.outreach_status NOT IN ('IN_CADENCE','CONVERTED','DEAD',
                                            'SKIPPED','SKIP','NURTURE'))
          AND ct.id IS NULL
        ORDER BY l.fit_score DESC
        """,
    ).fetchall()

    new_leads = []
    for r in new_rows:
        lead = dict(r)
        lead["phone"] = _best_phone(lead)
        lead["city"]  = _city_from_address(lead.get("principal_office_address") or "")
        try:
            formed = date.fromisoformat(lead["formation_date"])
            lead["formation_days_ago"] = (date.today() - formed).days
        except Exception:
            lead["formation_days_ago"] = None
        new_leads.append(lead)

    new_leads_count = len(new_leads)

    # When filter=new, collapse the queue to highlight triage
    if active_filter == "new":
        queue = []

    # ── Done today ───────────────────────────────────────────────────
    done_rows = conn.execute(
        """
        SELECT ct.id, ct.control_number, ct.step, ct.task_type,
               ct.label AS action_label, ct.outcome, ct.completed_at,
               l.entity_name
        FROM cadence_tasks ct
        JOIN leads l ON ct.control_number = l.control_number
        WHERE ct.status = 'done' AND date(ct.completed_at) = ?
        ORDER BY ct.completed_at DESC
        """,
        (today_str,),
    ).fetchall()
    done_today = [dict(r) for r in done_rows]

    conn.close()
    return render_template(
        "dashboard.html",
        due_today_count=due_today_count,
        new_leads_count=new_leads_count,
        overdue_count=overdue_count,
        new_leads=new_leads,
        queue=queue,
        done_today=done_today,
        selected_date=selected_str,
        selected_date_display=selected.strftime("%A, %B %d"),
        active_filter=active_filter,
    )


# ---------------------------------------------------------------------------
# Routes — Leads
# ---------------------------------------------------------------------------

_VALID_PRIORITIES = {"ALL", "HOT", "WARM", "COLD", "SKIP"}
_VALID_STATUSES   = {"active", "closed", "all"}


@app.route("/leads")
def leads():
    conn  = db()
    status_filter   = request.args.get("status", "active")
    priority_filter = request.args.get("priority", "ALL")

    if priority_filter not in _VALID_PRIORITIES:
        priority_filter = "ALL"
    if status_filter not in _VALID_STATUSES:
        status_filter = "active"

    params: list = []
    sql = "SELECT * FROM leads WHERE priority != 'SKIP'"
    if priority_filter != "ALL":
        sql += " AND priority = ?"
        params.append(priority_filter)
    if status_filter == "active":
        sql += " AND outreach_status NOT IN ('CONVERTED','DEAD')"
    elif status_filter == "closed":
        sql += " AND outreach_status IN ('CONVERTED','DEAD','NURTURE')"
    sql += " ORDER BY fit_score DESC"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Batch-load all cadence tasks to avoid N+1 queries
    if rows:
        cns = [r["control_number"] for r in rows]
        placeholders = ",".join("?" * len(cns))
        all_tasks = conn.execute(
            f"SELECT * FROM cadence_tasks WHERE control_number IN ({placeholders}) ORDER BY step",
            cns,
        ).fetchall()
        from collections import defaultdict
        tasks_by_cn: dict = defaultdict(list)
        for t in all_tasks:
            tasks_by_cn[t["control_number"]].append(dict(t))

        for lead in rows:
            tasks = tasks_by_cn.get(lead["control_number"], [])
            completed = sum(1 for t in tasks if t["status"] == "done")
            next_task = next((t for t in tasks if t["status"] == "pending"), None)
            lead["_cadence"] = {
                "active":    bool(tasks),
                "tasks":     tasks,
                "next_task": next_task,
                "completed": completed,
                "total":     len(tasks),
            }

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

    # Build rendered drafts — pass log history for adaptive copy
    best_ph = _best_phone(lead)
    best_email = lead.get("filer_email") or lead.get("business_email") or ""
    rendered = {}
    for step, tmpl in templates.items():
        subj = _render_template_body(tmpl["subject"], lead, log_list)
        body = _render_template_body(tmpl["body"],    lead, log_list)
        ttype = "vm" if step == 2 else "sms" if step == 5 else "email"
        rendered[step] = {
            "subject":  subj,
            "body":     body,
            "type":     ttype,
            "mailto":   _make_mailto(best_email, subj, body) if ttype == "email" else "",
            "sms":      _make_sms(best_ph or "", body)       if ttype in ("sms", "vm") else "",
            "tel":      f"tel:{best_ph}" if best_ph else "",
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
        best_phone=best_ph,
        best_email=best_email,
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
    if request.headers.get("HX-Request"):
        return "", 200
    return redirect(url_for("lead_detail", cn=cn))


@app.route("/leads/<cn>/complete_task", methods=["POST"])
def complete_task(cn: str):
    raw_task_id = request.form.get("task_id", "").strip()
    if not raw_task_id.isdigit():
        return "Invalid task_id", 400
    task_id = int(raw_task_id)
    outcome = request.form.get("outcome", "")
    notes   = request.form.get("notes", "")
    is_htmx = bool(request.headers.get("HX-Request"))

    conn = db()
    task = conn.execute(
        "SELECT * FROM cadence_tasks WHERE id=? AND control_number=?",
        (task_id, cn),
    ).fetchone()

    if task:
        if outcome == "bump":
            # Defer task by 1 day without completing it
            conn.execute(
                "UPDATE cadence_tasks SET due_date=date(due_date, '+1 day') WHERE id=?",
                (task_id,),
            )
        else:
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
            conn.execute(
                "UPDATE leads SET cadence_step=?, last_updated=CURRENT_TIMESTAMP WHERE control_number=?",
                (task["step"] + 1, cn),
            )
    conn.commit()
    conn.close()

    if is_htmx:
        return "", 200
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
    if request.headers.get("HX-Request"):
        return "", 200
    return redirect(url_for("lead_detail", cn=cn))


# ---------------------------------------------------------------------------
# Routes — Per-lead skip + email-opened (Today screen)
# ---------------------------------------------------------------------------

@app.route("/leads/<cn>/skip", methods=["POST"])
def skip_lead(cn: str):
    if not re.match(r'^[A-Za-z0-9_\-]{1,40}$', cn):
        return "Invalid control number", 400
    conn = db()
    exists = conn.execute("SELECT 1 FROM leads WHERE control_number=?", (cn,)).fetchone()
    if not exists:
        conn.close()
        return "Lead not found", 404
    conn.execute(
        "UPDATE cadence_tasks SET status='skipped' WHERE control_number=? AND status='pending'",
        (cn,),
    )
    conn.execute(
        """UPDATE leads SET outreach_status='SKIPPED', last_updated=CURRENT_TIMESTAMP
           WHERE control_number=?""",
        (cn,),
    )
    conn.execute(
        "INSERT INTO outreach_log (control_number, task_type, outcome) VALUES (?, 'outcome', 'skipped')",
        (cn,),
    )
    conn.commit()
    conn.close()
    if request.headers.get("HX-Request"):
        return "", 200
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/leads/<cn>/email_opened", methods=["POST"])
def email_opened(cn: str):
    if not re.match(r'^[A-Za-z0-9_\-]{1,40}$', cn):
        return "Invalid control number", 400
    conn = db()
    exists = conn.execute("SELECT 1 FROM leads WHERE control_number=?", (cn,)).fetchone()
    if not exists:
        conn.close()
        return "Lead not found", 404
    conn.execute(
        "INSERT INTO outreach_log (control_number, task_type, outcome) VALUES (?, 'email', 'email_opened_in_client')",
        (cn,),
    )
    conn.commit()
    conn.close()
    return "", 200


# ---------------------------------------------------------------------------
# Routes — Bulk lead actions (Today screen triage)
# ---------------------------------------------------------------------------

@app.route("/leads/bulk_start", methods=["POST"])
def bulk_start():
    raw = request.form.get("control_numbers", "")
    cns = [
        cn.strip() for cn in raw.split(",")
        if re.match(r'^[A-Za-z0-9_\-]{1,40}$', cn.strip())
    ]
    if not cns:
        return "No valid control numbers", 400
    today = date.today()
    conn = db()
    for cn in cns:
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
            """UPDATE leads SET outreach_status='IN_CADENCE', cadence_start_date=?,
               cadence_step=1, last_updated=CURRENT_TIMESTAMP WHERE control_number=?""",
            (str(today), cn),
        )
    conn.commit()
    conn.close()
    if request.headers.get("HX-Request"):
        return "", 200
    return redirect(url_for("dashboard"))


@app.route("/leads/bulk_skip", methods=["POST"])
def bulk_skip():
    raw = request.form.get("control_numbers", "")
    cns = [
        cn.strip() for cn in raw.split(",")
        if re.match(r'^[A-Za-z0-9_\-]{1,40}$', cn.strip())
    ]
    if not cns:
        return "No valid control numbers", 400
    conn = db()
    for cn in cns:
        conn.execute(
            "UPDATE cadence_tasks SET status='skipped' WHERE control_number=? AND status='pending'",
            (cn,),
        )
        conn.execute(
            """UPDATE leads SET outreach_status='SKIPPED', last_updated=CURRENT_TIMESTAMP
               WHERE control_number=?""",
            (cn,),
        )
        conn.execute(
            "INSERT INTO outreach_log (control_number, task_type, outcome) VALUES (?, 'outcome', 'skipped')",
            (cn,),
        )
    conn.commit()
    conn.close()
    if request.headers.get("HX-Request"):
        return "", 200
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes — Batch lead actions (kanban multi-select)
# ---------------------------------------------------------------------------

@app.route("/leads/batch", methods=["POST"])
def batch_leads():
    raw    = request.form.get("control_numbers", "")
    action = request.form.get("action", "")
    if action not in ("close", "skip", "start_cadence"):
        return "Invalid action", 400

    cns = [
        cn.strip() for cn in raw.split(",")
        if re.match(r'^[A-Za-z0-9_\-]{1,40}$', cn.strip())
    ]
    if not cns:
        return redirect(url_for("pipeline_kanban"))

    conn  = db()
    today = date.today()

    if action == "close":
        for cn in cns:
            conn.execute(
                "UPDATE cadence_tasks SET status='skipped' WHERE control_number=? AND status='pending'",
                (cn,),
            )
            conn.execute(
                "UPDATE leads SET outreach_status='DEAD', last_updated=CURRENT_TIMESTAMP WHERE control_number=?",
                (cn,),
            )
            conn.execute(
                "INSERT INTO outreach_log (control_number, task_type, outcome, notes) VALUES (?, 'outcome', 'dead', 'Batch closed')",
                (cn,),
            )
    elif action == "skip":
        for cn in cns:
            conn.execute(
                "UPDATE leads SET priority='SKIP', last_updated=CURRENT_TIMESTAMP WHERE control_number=?",
                (cn,),
            )
    elif action == "start_cadence":
        for cn in cns:
            has_tasks = conn.execute(
                "SELECT COUNT(*) FROM cadence_tasks WHERE control_number=?", (cn,)
            ).fetchone()[0]
            if has_tasks:
                continue
            for touch in CADENCE:
                due = today + timedelta(days=touch["day"] - 1)
                conn.execute(
                    """INSERT INTO cadence_tasks
                       (control_number, step, cadence_day, task_type, label, due_date)
                       VALUES (?,?,?,?,?,?)""",
                    (cn, touch["step"], touch["day"], touch["type"], touch["label"], str(due)),
                )
            conn.execute(
                """UPDATE leads SET outreach_status='IN_CADENCE', cadence_start_date=?,
                   cadence_step=1, last_updated=CURRENT_TIMESTAMP WHERE control_number=?""",
                (str(today), cn),
            )

    conn.commit()
    conn.close()
    return redirect(url_for("pipeline_kanban"))


# ---------------------------------------------------------------------------
# Routes — Quick notes from dashboard
# ---------------------------------------------------------------------------

@app.route("/leads/<cn>/note", methods=["POST"])
def add_note(cn: str):
    note = request.form.get("note", "").strip()
    if note:
        conn = db()
        conn.execute(
            """INSERT INTO outreach_log (control_number, task_type, outcome, notes)
               VALUES (?, 'note', 'note', ?)""",
            (cn, note),
        )
        conn.execute(
            "UPDATE leads SET last_updated=CURRENT_TIMESTAMP WHERE control_number=?",
            (cn,),
        )
        conn.commit()
        conn.close()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/leads/<cn>/reschedule_task", methods=["POST"])
def reschedule_task(cn: str):
    raw_task_id = request.form.get("task_id", "").strip()
    new_date    = request.form.get("due_date", "").strip()
    if not raw_task_id.isdigit():
        return "Invalid task_id", 400
    try:
        datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid date", 400
    task_id = int(raw_task_id)
    conn = db()
    conn.execute(
        "UPDATE cadence_tasks SET due_date=? WHERE id=? AND control_number=? AND status='pending'",
        (new_date, task_id, cn),
    )
    conn.commit()
    conn.close()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/today/list")
def today_list():
    conn = db()
    tasks = _today_tasks(conn)
    conn.close()
    for task in tasks:
        ikey = _industry_key(task.get("industry_category") or "")
        task["_pain_point"] = INDUSTRY_PAIN_POINTS.get(ikey, INDUSTRY_PAIN_POINTS["default"])
        task["_phone"]      = task.get("filer_phone") or task.get("business_phone") or ""
        task["_first"]      = (task.get("organizer_name") or "there").split()[0].title()
    return render_template(
        "today_list.html",
        tasks=tasks,
        today=date.today().strftime("%A, %B %d"),
    )


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


_VALID_TEMPLATE_STEPS = {2, 3, 5, 8, 10, 12}


@app.route("/templates/save", methods=["POST"])
def save_template():
    try:
        step = int(request.form["step"])
    except (KeyError, ValueError):
        return "Invalid step", 400
    if step not in _VALID_TEMPLATE_STEPS:
        return "Invalid step", 400
    subject = request.form.get("subject", "")
    body    = request.form.get("body", "")

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
# Routes — Inline field editing
# ---------------------------------------------------------------------------

@app.route("/leads/<cn>/edit", methods=["POST"])
def edit_lead(cn: str):
    EDITABLE = {"filer_phone", "filer_email", "business_phone", "website", "notes", "organizer_name"}
    data = request.get_json(silent=True) or {}
    updates = {k: v for k, v in data.items() if k in EDITABLE}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    conn = db()
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE leads SET {sets}, last_updated = CURRENT_TIMESTAMP WHERE control_number = ?",
        list(updates.values()) + [cn],
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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
    _headless = os.environ.get("BROWSER_HEADLESS", "0") != "0"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=_headless,
            args=([] if _headless else ["--window-position=3000,3000"]),
        )
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
    if next_step <= 12:
        return "day12"
    return "closed"


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
        ORDER BY (l.fit_score * 1.0 / MAX(1, CAST(julianday('now') - julianday(l.formation_date) AS INTEGER))) DESC
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
# Routes — Analytics
# ---------------------------------------------------------------------------

@app.route("/analytics")
def analytics():
    conn = db()

    totals = conn.execute(
        """SELECT COUNT(*) as total,
              COALESCE(SUM(CASE WHEN outreach_status='CONVERTED'              THEN 1 ELSE 0 END), 0) as converted,
              COALESCE(SUM(CASE WHEN outreach_status IN ('DEAD','NO_CONTACT') THEN 1 ELSE 0 END), 0) as dead,
              COALESCE(SUM(CASE WHEN outreach_status='IN_CADENCE'             THEN 1 ELSE 0 END), 0) as in_cadence,
              COALESCE(SUM(CASE WHEN priority='HOT'  THEN 1 ELSE 0 END), 0) as hot,
              COALESCE(SUM(CASE WHEN priority='WARM' THEN 1 ELSE 0 END), 0) as warm
           FROM leads WHERE priority != 'SKIP'"""
    ).fetchone()

    industry_pipeline = conn.execute(
        """SELECT industry_category, priority, COUNT(*) as n
           FROM leads
           WHERE priority NOT IN ('SKIP') AND industry_category IS NOT NULL
           GROUP BY industry_category, priority"""
    ).fetchall()

    industry_outcomes = conn.execute(
        """SELECT industry_category, outreach_status, COUNT(*) as n
           FROM leads
           WHERE outreach_status IN ('CONVERTED','DEAD','NO_CONTACT','NURTURE')
             AND industry_category IS NOT NULL
           GROUP BY industry_category, outreach_status"""
    ).fetchall()

    priority_outcomes = conn.execute(
        """SELECT priority, outreach_status, COUNT(*) as n
           FROM leads
           WHERE outreach_status IN ('CONVERTED','DEAD','NO_CONTACT')
             AND priority IN ('HOT','WARM','COLD')
           GROUP BY priority, outreach_status"""
    ).fetchall()

    dropoff = conn.execute(
        """SELECT cadence_step, COUNT(*) as n
           FROM leads
           WHERE outreach_status IN ('DEAD','NO_CONTACT')
             AND cadence_step IS NOT NULL AND cadence_step > 0
           GROUP BY cadence_step ORDER BY cadence_step"""
    ).fetchall()

    weekly = conn.execute(
        """SELECT strftime('%Y-%m-%d', first_seen, 'weekday 1', '-6 days') as week_start,
                  COUNT(*) as n,
                  SUM(CASE WHEN priority='HOT'  THEN 1 ELSE 0 END) as hot,
                  SUM(CASE WHEN priority='WARM' THEN 1 ELSE 0 END) as warm
           FROM leads
           WHERE priority != 'SKIP' AND first_seen IS NOT NULL
           GROUP BY week_start ORDER BY week_start DESC LIMIT 8"""
    ).fetchall()

    avg_row = conn.execute(
        """SELECT AVG(touch_count) as avg FROM (
             SELECT ol.control_number, COUNT(*) as touch_count
             FROM outreach_log ol
             JOIN leads l ON ol.control_number = l.control_number
             WHERE l.outreach_status='CONVERTED'
               AND ol.task_type NOT IN ('outcome','note')
             GROUP BY ol.control_number
           )"""
    ).fetchone()

    conn.close()

    # Build industry table
    industry_data: dict = {}
    for row in industry_pipeline:
        cat = row["industry_category"] or "Unknown"
        industry_data.setdefault(cat, {"HOT": 0, "WARM": 0, "COLD": 0, "converted": 0, "dead": 0})
        industry_data[cat][row["priority"]] = row["n"]
    for row in industry_outcomes:
        cat = row["industry_category"] or "Unknown"
        industry_data.setdefault(cat, {"HOT": 0, "WARM": 0, "COLD": 0, "converted": 0, "dead": 0})
        s = row["outreach_status"]
        if s in ("DEAD", "NO_CONTACT"):
            industry_data[cat]["dead"] += row["n"]
        elif s == "CONVERTED":
            industry_data[cat]["converted"] += row["n"]

    industry_list = []
    for cat, d in industry_data.items():
        closed = d["converted"] + d["dead"]
        industry_list.append({
            "category": cat,
            **d,
            "total_active": d["HOT"] + d["WARM"] + d["COLD"],
            "rate": round(d["converted"] / closed * 100) if closed else None,
        })
    industry_list.sort(key=lambda x: (x["converted"] * 10 + x["HOT"]), reverse=True)

    # Priority accuracy
    accuracy: dict = {}
    for row in priority_outcomes:
        p = row["priority"]
        s = row["outreach_status"]
        accuracy.setdefault(p, {"CONVERTED": 0, "DEAD": 0, "NO_CONTACT": 0})
        accuracy[p][s] = row["n"]

    return render_template(
        "analytics.html",
        totals=dict(totals),
        industry_list=industry_list,
        accuracy=accuracy,
        dropoff=[dict(r) for r in dropoff],
        weekly=[dict(r) for r in weekly],
        avg_touches=round(avg_row["avg"], 1) if avg_row and avg_row["avg"] else None,
    )


# ---------------------------------------------------------------------------
# Routes — Notifications
# ---------------------------------------------------------------------------

@app.route("/notifications/read", methods=["POST"])
def notifications_read():
    mark_all_read()
    from urllib.parse import urlparse
    ref = request.referrer or "/"
    if urlparse(ref).netloc not in ("", request.host):
        ref = "/"
    return redirect(ref)


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
    port = int(os.environ.get("PORT", "5001"))
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", port=port)
