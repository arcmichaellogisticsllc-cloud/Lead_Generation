"""Create and read in-app notifications stored in SQLite."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db import DB_PATH, get_connection

logger = logging.getLogger(__name__)

ICONS = {
    "new_lead":     "⚡",
    "overdue_task": "⚠️",
    "stale_lead":   "⏰",
    "pipeline_run": "✅",
}


def create(
    title: str,
    body: str = "",
    link: str = "",
    ntype: str = "pipeline_run",
    db_path: Path = DB_PATH,
) -> None:
    try:
        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO notifications (type, title, body, link) VALUES (?,?,?,?)",
            (ntype, title, body, link),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to create notification: %s", exc)


def get_unread(limit: int = 20, db_path: Path = DB_PATH) -> list[dict]:
    try:
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT * FROM notifications WHERE read=0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["icon"] = ICONS.get(d.get("type", ""), "📋")
            result.append(d)
        return result
    except Exception:
        return []


def unread_count(db_path: Path = DB_PATH) -> int:
    try:
        conn = get_connection(db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE read=0"
        ).fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def mark_all_read(db_path: Path = DB_PATH) -> None:
    try:
        conn = get_connection(db_path)
        conn.execute("UPDATE notifications SET read=1 WHERE read=0")
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to mark notifications read: %s", exc)
