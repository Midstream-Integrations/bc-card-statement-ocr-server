"""SQLite connection shared by every module (one database file: DATA_DIR/ocr.db).
Each module keeps its own tables and creates/migrates them in its own store.init()."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from core.settings import DB_PATH


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def add_column(c, table, column, decl):
    """Idempotent ALTER TABLE ... ADD COLUMN for schema upgrades."""
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
