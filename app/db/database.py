import sqlite3
import threading
from contextlib import contextmanager

from app.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'morning',
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    time_window_start TEXT NOT NULL DEFAULT '06:00',
    time_window_end TEXT NOT NULL DEFAULT '10:00',
    interval_minutes INTEGER NOT NULL DEFAULT 10,
    weekdays TEXT NOT NULL DEFAULT 'Mon,Tue,Wed,Thu,Fri',
    arrival_deadline TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
