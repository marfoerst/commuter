import sqlite3
import threading
from contextlib import contextmanager

from app.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    api_token TEXT UNIQUE,
    ntfy_topic_url TEXT,
    webhook_url TEXT,
    push_min_severity TEXT NOT NULL DEFAULT 'alert',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Per-user, per-day count of Google Routes API calls spent. Enforces the shared
-- key's per-user budget so one user can't drain the free tier for everyone.
CREATE TABLE IF NOT EXISTS api_usage (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL DEFAULT 'morning',
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    time_window_start TEXT NOT NULL DEFAULT '06:00',
    time_window_end TEXT NOT NULL DEFAULT '10:00',
    interval_minutes INTEGER NOT NULL DEFAULT 10,
    weekdays TEXT NOT NULL DEFAULT 'Mon,Tue,Wed,Thu,Fri',
    arrival_deadline TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- One active route per (user, name). Partial index so legacy rows with a NULL
-- user_id (pre-migration) don't collide before they're backfilled at startup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_routes_user_name_active
ON routes(user_id, name) WHERE is_active = 1 AND user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS commute_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,
    departure_time TEXT NOT NULL,
    duration_minutes REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_commute_route_day
ON commute_data(route_id, day_of_week, departure_time);

-- Append-only history of every duration we have observed for a slot, from both
-- the daily batch ('batch') and the live re-rank probes ('live'). commute_data
-- holds only the latest forecast per slot (it is replaced each day); this table
-- accumulates over time so we can compute typical/p90 durations, detect days
-- that are worse than typical, and honour a post-event baseline reset.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,
    departure_time TEXT NOT NULL,
    duration_minutes REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'batch',
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_obs_route_slot
ON observations(route_id, day_of_week, departure_time, observed_at);
"""

_write_lock = threading.Lock()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent migrations on top of the base SCHEMA."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(routes)").fetchall()}
    if "name" not in cols:
        conn.execute("ALTER TABLE routes ADD COLUMN name TEXT NOT NULL DEFAULT 'morning'")
        conn.execute("UPDATE routes SET name = 'morning' WHERE name IS NULL OR name = ''")
    if "arrival_deadline" not in cols:
        conn.execute("ALTER TABLE routes ADD COLUMN arrival_deadline TEXT")
    if "baseline_since" not in cols:
        # Date (YYYY-MM-DD) of a traffic-changing event (e.g. a bridge closure).
        # When set, typical/p90 and incident baselines ignore observations from
        # before this date so the pre-event world stops contaminating the stats.
        conn.execute("ALTER TABLE routes ADD COLUMN baseline_since TEXT")
    if "bonn_segment_ids" not in cols:
        # JSON list of Bonn open-data strecke_ids that lie along this route,
        # computed by matching the route geometry to the live traffic feed at
        # config/recompute time. Drives the live local-traffic panel + alerts.
        conn.execute("ALTER TABLE routes ADD COLUMN bonn_segment_ids TEXT")
    if "user_id" not in cols:
        # Multi-user: scope each route to an owning user. Existing single-tenant
        # rows get NULL here and are backfilled to the seeded admin at startup
        # (see app.main.seed_admin_user), after which the unique index applies.
        conn.execute("ALTER TABLE routes ADD COLUMN user_id INTEGER")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def get_conn():
    conn = sqlite3.connect(
        str(DATABASE_PATH),
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        with _write_lock:
            yield conn
            conn.commit()
    finally:
        conn.close()
