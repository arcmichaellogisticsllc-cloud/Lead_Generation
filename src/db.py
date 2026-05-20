from __future__ import annotations
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "leads.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    # Add cadence columns if they don't exist yet (idempotent ALTER TABLE)
    for col, definition in [
        ("cadence_start_date", "DATE"),
        ("cadence_step",       "INTEGER DEFAULT 0"),
        ("instagram_url",      "TEXT"),
        ("yelp_url",           "TEXT"),
        ("angi_url",           "TEXT"),
        ("google_maps_url",    "TEXT"),
        ("profiles_searched",  "BOOLEAN DEFAULT 0"),
        ("review_snippet",     "TEXT"),
        ("review_source",      "TEXT"),
        ("review_has_payment_friction", "BOOLEAN DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            pass  # column already exists

    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
              control_number TEXT PRIMARY KEY,
              entity_name TEXT NOT NULL,
              entity_type TEXT,
              status TEXT,
              formation_date DATE,
              naics_code TEXT,
              tier INTEGER,
              industry_category TEXT,

              registered_agent_name TEXT,
              registered_agent_address TEXT,
              registered_agent_email TEXT,
              registered_agent_is_service BOOLEAN DEFAULT 0,

              organizer_name TEXT,
              organizer_address TEXT,
              organizer_capacity TEXT,

              filer_email TEXT,
              filer_phone TEXT,

              principal_office_address TEXT,
              business_email TEXT,

              website TEXT,
              business_phone TEXT,
              owner_personal_phone TEXT,
              owner_email TEXT,
              google_place_id TEXT,
              facebook_url TEXT,
              linkedin_url TEXT,
              ga_license_number TEXT,

              has_website BOOLEAN,
              has_online_booking BOOLEAN,
              has_online_payment BOOLEAN,
              detected_payment_processor TEXT,
              detected_vertical_saas TEXT,
              invoice_workflow_signals TEXT,

              fit_score INTEGER,
              score_breakdown TEXT,
              priority TEXT,

              outreach_status TEXT DEFAULT 'NEW',
              notes TEXT,

              first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_leads_formation_date ON leads(formation_date);
            CREATE INDEX IF NOT EXISTS idx_leads_tier ON leads(tier);
            CREATE INDEX IF NOT EXISTS idx_leads_priority ON leads(priority);
            CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(fit_score DESC);

            -- cadence columns added in v2 (ignore if already exist)


            CREATE TABLE IF NOT EXISTS cadence_tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              control_number TEXT NOT NULL REFERENCES leads(control_number),
              step INTEGER NOT NULL,
              cadence_day INTEGER NOT NULL,
              task_type TEXT NOT NULL,
              label TEXT NOT NULL,
              due_date DATE NOT NULL,
              status TEXT DEFAULT 'pending',
              completed_at TIMESTAMP,
              outcome TEXT,
              notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_cn     ON cadence_tasks(control_number);
            CREATE INDEX IF NOT EXISTS idx_tasks_due    ON cadence_tasks(due_date);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON cadence_tasks(status);

            CREATE TABLE IF NOT EXISTS outreach_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              control_number TEXT NOT NULL REFERENCES leads(control_number),
              step INTEGER,
              cadence_day INTEGER,
              task_type TEXT NOT NULL,
              outcome TEXT,
              notes TEXT,
              logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS email_templates (
              step INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              subject TEXT,
              body TEXT NOT NULL,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pipeline_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              date_range_start DATE,
              date_range_end DATE,
              entities_discovered INTEGER,
              entities_kept INTEGER,
              pdfs_downloaded INTEGER,
              enrichment_completed INTEGER,
              errors TEXT
            );

            CREATE TABLE IF NOT EXISTS notifications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL DEFAULT 'pipeline_run',
              title TEXT NOT NULL,
              body TEXT DEFAULT '',
              link TEXT DEFAULT '',
              read BOOLEAN DEFAULT 0,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_notif_read ON notifications(read, created_at DESC);
        """)
    conn.close()


def upsert_lead(conn: sqlite3.Connection, lead: dict) -> None:
    fields = list(lead.keys())
    placeholders = ", ".join(["?" for _ in fields])
    updates = ", ".join([f"{f} = excluded.{f}" for f in fields if f != "control_number"])
    sql = f"""
        INSERT INTO leads ({', '.join(fields)})
        VALUES ({placeholders})
        ON CONFLICT(control_number) DO UPDATE SET
            {updates},
            last_updated = CURRENT_TIMESTAMP
    """
    conn.execute(sql, list(lead.values()))


def get_lead(conn: sqlite3.Connection, control_number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leads WHERE control_number = ?", (control_number,)
    ).fetchone()


def leads_missing_field(conn: sqlite3.Connection, field: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM leads WHERE {field} IS NULL ORDER BY first_seen DESC"
    ).fetchall()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
    conn = get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    print("Tables:", [t["name"] for t in tables])
    conn.close()
