import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "queue.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    hook TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    brief_ref TEXT NOT NULL DEFAULT '',
    link_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at TEXT,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    published_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    message TEXT NOT NULL,
    intent TEXT NOT NULL,
    post_id INTEGER,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    followers_gained INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    customers INTEGER NOT NULL DEFAULT 0,
    revenue REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    message TEXT NOT NULL,
    intent TEXT NOT NULL,
    post_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    views INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    post_id INTEGER,
    link_id TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    post_a INTEGER NOT NULL,
    post_b INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    priority TEXT NOT NULL,
    route TEXT NOT NULL,
    text TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE(kind, ref)
);
"""

# (table, column, definition) added after first release; applied if missing.
MIGRATIONS = (
    ("posts", "payload", "TEXT NOT NULL DEFAULT ''"),
    ("posts", "warnings", "TEXT NOT NULL DEFAULT ''"),
    ("leads", "source_link_id", "TEXT NOT NULL DEFAULT ''"),
    # archived_at: hidden from normal views but kept for the audit trail. is_test: removable by the cleanup button.
    ("posts", "archived_at", "TEXT"),
    ("posts", "is_test", "INTEGER NOT NULL DEFAULT 0"),
    ("inbound", "archived_at", "TEXT"),
    ("inbound", "is_test", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "archived_at", "TEXT"),
    ("leads", "is_test", "INTEGER NOT NULL DEFAULT 0"),
    # approved_hash: what exactly the human approved; posts approved before this column existed must be re-approved.
    ("posts", "approved_hash", "TEXT"),
)


def _pending_migrations(conn):
    pending = []
    for table, col, ddl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            pending.append((table, col, ddl))
    return pending


def _backup_before_migration(conn, path):
    """Copies the database next to itself (data/backups/) before the schema changes. Returns the backup path."""
    from datetime import datetime
    backups = path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest_path = backups / f"queue-before-migration-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    dest = sqlite3.connect(str(dest_path))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return dest_path


def _migrate(conn, path=None):
    """Adds missing columns. An existing database with data is backed up first; a brand new one is not."""
    pending = _pending_migrations(conn)
    if not pending:
        return
    if path is not None and str(path) != ":memory:" and conn.execute("SELECT 1 FROM posts LIMIT 1").fetchone():
        _backup_before_migration(conn, Path(path))
    for table, col, ddl in pending:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()


def connect(path=None):
    path = Path(path or os.environ.get("ORKI_DB") or DEFAULT_DB)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn, path)
    return conn
