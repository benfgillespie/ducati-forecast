"""Database layer. Talks to either SQLite (local dev) or Postgres (Vercel) based on
the DATABASE_URL env var. Most queries use `?` placeholders and go through the
`q()` helper, which converts to psycopg's `%s` style when running against Postgres."""

import os
import re
import sqlite3
from pathlib import Path

from flask import g

def _clean_postgres_url(url: str) -> str:
    """Supabase's POSTGRES_URL can include non-standard query params (e.g. `supa=`)
    that psycopg's libpq parser rejects. Drop everything except a small allow-list
    of well-known libpq options."""
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    p = urlparse(url)
    if not p.query:
        return url
    allowed = {
        "sslmode", "sslcert", "sslkey", "sslrootcert", "channel_binding",
        "application_name", "connect_timeout", "options", "target_session_attrs",
    }
    keep = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k in allowed]
    return urlunparse(p._replace(query=urlencode(keep)))


DATABASE_URL = _clean_postgres_url(
    (os.environ.get("DATABASE_URL")
     or os.environ.get("POSTGRES_URL")
     or "").strip()
)
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = Path(__file__).parent / "data" / "dashboard.db"

if USE_POSTGRES:
    import psycopg  # psycopg3
    from psycopg.rows import dict_row


# --- schema -----------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dealers (
    id INTEGER PRIMARY KEY,
    password TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    dealer_code TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS material_map (
    id INTEGER PRIMARY KEY,
    material_prefix TEXT UNIQUE NOT NULL,
    plan_super TEXT,
    plan_model TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    proposed_plan_super TEXT,
    proposed_plan_model TEXT,
    proposal_rejected INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plan (
    id INTEGER PRIMARY KEY,
    country TEXT NOT NULL,
    plan_super TEXT,
    plan_model TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plan_lookup ON plan(country, plan_model, year, month);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    order_number TEXT,
    material_prefix TEXT,
    material_full TEXT,
    bike_super_model TEXT,
    bike_model TEXT,
    bike_color TEXT,
    bike_type TEXT,
    end_customer_status TEXT,
    country TEXT,
    dealer TEXT,
    dealer_code TEXT,
    request_date TEXT,
    order_creation_date TEXT,
    confirmed_delivery_date TEXT,
    order_status_group TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_lookup ON orders(country, material_prefix);
CREATE INDEX IF NOT EXISTS idx_orders_dealer ON orders(dealer_code);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    plan_filename TEXT,
    orders_filename TEXT,
    plan_rows INTEGER,
    order_rows INTEGER,
    unmapped_count INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS embargoes (
    plan_model TEXT PRIMARY KEY,
    embargo_until TEXT,
    manually_hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plan_model_history (
    plan_model TEXT PRIMARY KEY,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS material_map_backups (
    id INTEGER PRIMARY KEY,
    snapshot_at TEXT DEFAULT CURRENT_TIMESTAMP,
    label TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY,
    material_prefix TEXT,
    material_full TEXT,
    bike_super_model TEXT,
    bike_model TEXT,
    country TEXT,
    order_number TEXT,
    dealer_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocations_lookup ON allocations(country, material_prefix);
CREATE TABLE IF NOT EXISTS allocation_reports (
    id INTEGER PRIMARY KEY,
    report_date TEXT NOT NULL,
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    filename TEXT,
    row_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    submitter TEXT,
    page TEXT,
    message TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS dealers (
    id BIGSERIAL PRIMARY KEY,
    password TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    dealer_code TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS material_map (
    id BIGSERIAL PRIMARY KEY,
    material_prefix TEXT UNIQUE NOT NULL,
    plan_super TEXT,
    plan_model TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    proposed_plan_super TEXT,
    proposed_plan_model TEXT,
    proposal_rejected INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plan (
    id BIGSERIAL PRIMARY KEY,
    country TEXT NOT NULL,
    plan_super TEXT,
    plan_model TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plan_lookup ON plan(country, plan_model, year, month);
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    order_number TEXT,
    material_prefix TEXT,
    material_full TEXT,
    bike_super_model TEXT,
    bike_model TEXT,
    bike_color TEXT,
    bike_type TEXT,
    end_customer_status TEXT,
    country TEXT,
    dealer TEXT,
    dealer_code TEXT,
    request_date TEXT,
    order_creation_date TEXT,
    confirmed_delivery_date TEXT,
    order_status_group TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_lookup ON orders(country, material_prefix);
CREATE INDEX IF NOT EXISTS idx_orders_dealer ON orders(dealer_code);
CREATE TABLE IF NOT EXISTS imports (
    id BIGSERIAL PRIMARY KEY,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    plan_filename TEXT,
    orders_filename TEXT,
    plan_rows INTEGER,
    order_rows INTEGER,
    unmapped_count INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS embargoes (
    plan_model TEXT PRIMARY KEY,
    embargo_until TEXT,
    manually_hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plan_model_history (
    plan_model TEXT PRIMARY KEY,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS material_map_backups (
    id BIGSERIAL PRIMARY KEY,
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    label TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    id BIGSERIAL PRIMARY KEY,
    material_prefix TEXT,
    material_full TEXT,
    bike_super_model TEXT,
    bike_model TEXT,
    country TEXT,
    order_number TEXT,
    dealer_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocations_lookup ON allocations(country, material_prefix);
CREATE TABLE IF NOT EXISTS allocation_reports (
    id BIGSERIAL PRIMARY KEY,
    report_date TEXT NOT NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    filename TEXT,
    row_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    submitter TEXT,
    page TEXT,
    message TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
"""


# --- query helpers ----------------------------------------------------

_NAMED_PARAM_RE = re.compile(r"(?<!:):(\w+)")


def q(sql: str) -> str:
    """Translate placeholders from SQLite style to psycopg style when on
    Postgres. SQLite uses `?` (positional) and `:name` (named); psycopg uses
    `%s` and `%(name)s`. The negative-lookbehind keeps `::cast` operators intact."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        sql = _NAMED_PARAM_RE.sub(r"%(\1)s", sql)
    return sql


def today_sql() -> str:
    """Dialect-appropriate SQL fragment for 'today's date' as an ISO-formatted
    string, so it can be compared against TEXT columns without a cast on either
    side blowing up."""
    return "to_char(CURRENT_DATE, 'YYYY-MM-DD')" if USE_POSTGRES else "date('now')"


def group_concat_distinct(col: str) -> str:
    """Dialect-appropriate aggregate for a comma-joined list of distinct values."""
    if USE_POSTGRES:
        return f"STRING_AGG(DISTINCT {col}::text, ',')"
    return f"GROUP_CONCAT(DISTINCT {col})"


# --- connection -------------------------------------------------------

def _connect():
    if USE_POSTGRES:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)
        # Disable psycopg3's automatic prepared-statement cache: the Vercel/Neon/
        # Supabase poolers run PgBouncer in transaction mode, which recycles backend
        # connections between clients. The client thinks "_pg3_N" already exists on
        # this session, but the new backend has never seen it — DuplicatePreparedStatement.
        conn.prepare_threshold = None
        return conn
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


class _Conn:
    """Thin shim so callers can use con.execute(sql, params) uniformly."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        cur = self.raw.cursor()
        cur.execute(q(sql), params)
        return cur

    def executemany(self, sql, seq):
        cur = self.raw.cursor()
        cur.executemany(q(sql), seq)
        return cur

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def get_conn():
    if "db" not in g:
        g.db = _Conn(_connect())
    return g.db


def close_conn(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- schema bootstrap -------------------------------------------------

def init_db():
    raw = _connect()
    schema = _POSTGRES_SCHEMA if USE_POSTGRES else _SQLITE_SCHEMA
    if USE_POSTGRES:
        with raw.cursor() as cur:
            cur.execute(schema)
    else:
        raw.executescript(schema)
    raw.commit()
    _migrate(raw)
    raw.close()


# Columns added after the initial schema. Listed here so older DBs catch up
# without losing data. Safe to re-run.
_POST_INIT_COLUMNS = [
    ("material_map", "proposed_plan_super",  "TEXT"),
    ("material_map", "proposed_plan_model",  "TEXT"),
    ("material_map", "proposal_rejected",    "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(raw):
    for table, column, coltype in _POST_INIT_COLUMNS:
        _add_column_if_missing(raw, table, column, coltype)
    raw.commit()


def _add_column_if_missing(raw, table, column, coltype):
    """ALTER TABLE ADD COLUMN, swallowing the dialect-specific 'already exists'
    error. SQLite doesn't support `IF NOT EXISTS` for ADD COLUMN; Postgres does
    but we keep the same try/except shape for symmetry."""
    cur = raw.cursor()
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "already exists" in msg:
            if USE_POSTGRES:
                raw.rollback()
            return
        raise


# --- settings helpers (used both at request time and during bootstrap) ---

def get_setting(key, default=None):
    row = get_conn().execute(q("SELECT value FROM settings WHERE key=?"), (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(key, value):
    con = get_conn()
    if USE_POSTGRES:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (key, value),
        )
    else:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    con.commit()
