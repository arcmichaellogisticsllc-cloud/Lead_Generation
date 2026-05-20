# GA Payment-Operations Lead Pipeline

Discovers newly formed Georgia business entities, filters by target industry, extracts contact information from filing documents, enriches with website and payment-stack data, and exports a prioritized lead list.

## Purpose

New LLCs filing in Georgia show up in the Secretary of State's eCorp portal. Most owner-operators have no online payment system, no booking tool, and no CRM — exactly the moment a payment-ops sales pitch lands best. This pipeline finds them within days of filing and scores them for fit.

## Target Industries

| Tier | Focus | Industries |
|------|-------|------------|
| 1 (70%) | Field Service | HVAC, Plumbing, Electrical, Roofing, Painting, Landscaping, Concrete, Garage Door |
| 2 (20%) | Automotive | Auto Repair, Body Shops, Car Washes, Tire Dealers |
| 3 (10%) | Recurring/Membership | Med Spas, Salons, Fitness Studios, Massage |

## Setup

```bash
# 1. Create and activate virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browser
playwright install chromium

# 4. Initialize the database
python -m src.db

# 5. Copy environment file
cp .env.example .env
```

## Pipeline Stages

Run individual stages or the full pipeline:

```bash
# Full pipeline (last 1 day)
python -m src.pipeline run-all --days 1

# Individual stages
python -m src.pipeline discover --days 7
python -m src.pipeline scrape
python -m src.pipeline extract
python -m src.pipeline score
python -m src.pipeline enrich
python -m src.pipeline export --priority HOT
```

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

## Export CSVs

Exports land in `data/exports/`. Each export run produces:

- `hot_leads_YYYY-MM-DD.csv` — Score ≥ 80, immediate outreach
- `warm_leads_YYYY-MM-DD.csv` — Score 60–79, follow-up queue
- `cold_leads_YYYY-MM-DD.csv` — Score 40–59, nurture list

Key columns: `control_number`, `entity_name`, `formation_date`, `tier`, `fit_score`, `priority`, `filer_email`, `filer_phone`, `website`, `detected_payment_processor`, `principal_office_address`.

## Polite-Scraper Commitment

- Minimum 1.5-second delay between all eCorp requests
- Single-threaded, no parallelism
- Real browser user-agent (Playwright Chromium)
- No scraping outside eCorp and public business websites

## Phase 2 TODOs

- [ ] Google Places API enrichment (`enrichment/google_places.py`)
- [ ] Georgia professional licensing DB lookup (`enrichment/ga_licensing.py`)
- [ ] Interactive weight tuning (`scripts/tune_scoring.py`)
- [ ] Weekly email digest (`scripts/weekly_report.py`)
- [ ] Deduplication against existing CRM contacts
# Lead_Generation
