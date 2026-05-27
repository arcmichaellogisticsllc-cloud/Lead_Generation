# GA Payment-Operations Lead Pipeline

Discovers newly formed Georgia LLCs from the Secretary of State's eCorp portal, scores them for payment-ops fit, and manages outreach via a 12-touch cadence dashboard.

## Purpose

New LLCs filing in Georgia appear in the eCorp portal within days of formation. Most owner-operators have no payment system, no booking tool, and no CRM — the ideal moment for a payment-ops pitch. This pipeline finds them, scores them, and tracks outreach from first call through close.

## Target Industries

| Tier | Focus | Industries |
|------|-------|------------|
| 1 (70%) | Field Service | HVAC, Plumbing, Electrical, Roofing, Painting, Landscaping, Concrete, Garage Door |
| 2 (20%) | Automotive | Auto Repair, Body Shops, Car Washes, Tire Dealers |
| 3 (10%) | Recurring/Membership | Med Spas, Salons, Fitness Studios, Massage |

## Setup

```bash
# 1. Clone and create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browser (used for eCorp scraping)
playwright install chromium

# 4. Initialize the database
python -m src.db

# 5. Configure environment
cp .env.example .env
# Edit .env — at minimum set FLASK_SECRET_KEY
```

## Running the Dashboard

```bash
python app.py
# Opens at http://localhost:5001
```

| Route | Purpose |
|-------|---------|
| `/` | Today's tasks — calls, emails, voicemails due |
| `/pipeline` | Kanban board — all active leads by stage |
| `/leads` | Table view with filters |
| `/leads/<id>` | Lead detail — cadence timeline, email drafts, enrichment |
| `/templates` | Edit the 12-step outreach templates |
| `/analytics` | Win rate, drop-off by cadence step, industry breakdown |
| `/today/list` | Printable numbered call list |
| `/runs` | Pipeline run history and notifications |

## Automated Scheduling (macOS)

Install four launchd jobs with:

```bash
python scripts/install_scheduler.py
```

| Job | Schedule | What it does |
|-----|----------|--------------|
| Daily full pipeline | 7:00 AM | Discover → score → enrich → notify |
| Quick scan | 1:00 PM | Lighter discovery pass |
| Morning digest | 7:30 AM | macOS notification summary of today's tasks |
| Score calibration | Monday 8:00 AM | Auto-tune scoring weights from outcomes |

## Scoring Model

Each lead receives a fit score 0–100:

| Component | Points |
|-----------|--------|
| Tier 1 NAICS match | 30 |
| Tier 1 keyword match | 25 |
| Tier 2 NAICS match | 20 |
| Tier 2 keyword match | 18 |
| Tier 3 NAICS match | 15 |
| Tier 3 keyword match | 12 |
| Individual organizer (not a company) | 10 |
| Residential registered agent | 10 |
| LLC (not Corp) | 5 |
| Filed within 14 days | 5 |
| Filed within 7 days (additive) | 3 |
| No website detected | +15 |
| No online payment detected | +10 |
| Invoice-language signals | +8 |
| Known payment processor (Square/Stripe/etc.) | −15 to −20 |
| Vertical SaaS detected | −10 to −15 |

**Priority thresholds:** HOT ≥ 80 · WARM ≥ 60 · COLD ≥ 40 · SKIP < 40

Weights live in `config/scoring_weights.yaml` and are auto-tuned weekly by `scripts/scoring_feedback.py`. Run manually with `python scripts/scoring_feedback.py --apply`.

## Environment Variables

Copy `.env.example` to `.env`. All vars are optional with sensible defaults except `FLASK_SECRET_KEY`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLASK_SECRET_KEY` | `changeme` | Session security — set a real value |
| `FLASK_DEBUG` | `0` | Enable Flask debug mode |
| `BROWSER_HEADLESS` | `0` | Run Playwright headless (set `1` on servers) |
| `DISCOVER_SCAN` | `500` | Max entities to scan per pipeline run |
| `SENDER_NAME` | `Marcus McGee` | Used as `{{sender_name}}` in templates |
| `SENDER_EMAIL` | `mmcgee@ippayware.com` | Used as `{{sender_email}}` in templates |
| `SENDER_PHONE` | _(empty)_ | Used as `{{sender_phone}}` in templates |
| `SENDER_COMPANY` | `IPPayware` | Used as `{{sender_company}}` in templates |
| `AUTO_CADENCE_HOT` | `0` | Auto-start cadence for HOT leads within 48 h |
| `ALERT_PHONE` | _(empty)_ | E.164 number for new HOT lead SMS alerts |
| `TWILIO_SID/TOKEN/FROM` | _(empty)_ | Required for SMS alerts |
| `GOOGLE_PLACES_API_KEY` | _(empty)_ | Phone/website fallback via Places API |

## Polite-Scraper Commitment

- Minimum 1.5-second delay between all eCorp requests
- Single-threaded, no parallelism
- Real browser user-agent (Playwright Chromium)
- No scraping outside eCorp and public business websites

## Pending

- Georgia professional licensing DB lookup (`enrichment/ga_licensing.py` — stubbed)
- Deduplication against external CRM contacts
