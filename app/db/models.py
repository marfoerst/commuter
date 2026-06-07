from datetime import date, timedelta
from typing import Iterable

from app.db.database import get_conn
from app.services.stats import summarize

# How far back the trailing window for typical/p90 reaches when no explicit
# baseline_since is set. Keeps the "typical" duration responsive to recent
# conditions rather than averaging in months-old data.
RECENT_DAYS = 35


def get_route_by_name(name: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM routes WHERE name = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_all_active_routes() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM routes WHERE is_active = 1 ORDER BY name"
        )
        return [dict(r) for r in cur.fetchall()]


def upsert_named_route(
    name: str,
    origin: str,
    destination: str,
    time_window_start: str,
    time_window_end: str,
    interval_minutes: int,
    weekdays: str,
    arrival_deadline: str | None = None,
    baseline_since: str | None = None,
) -> int:
    """Deactivate any previous row with this name, insert a new active one."""
    with get_conn() as conn:
        conn.execute("UPDATE routes SET is_active = 0 WHERE name = ?", (name,))
        cur = conn.execute(
            """
            INSERT INTO routes
                (name, origin, destination, time_window_start, time_window_end,
                 interval_minutes, weekdays, arrival_deadline, baseline_since,
                 is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                name,
                origin,
                destination,
                time_window_start,
                time_window_end,
                interval_minutes,
                weekdays,
                arrival_deadline,
                baseline_since,
            ),
        )
        return int(cur.lastrowid)


def clear_route_data(route_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM commute_data WHERE route_id = ?", (route_id,))


def clear_day_data(route_id: int, day_of_week: str) -> None:
    """Clear a single weekday's column, leaving the rest of the week intact.

    Used by the daily batch, which now refreshes only today's forecast rather
    than re-sampling the whole week every day.
    """
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM commute_data WHERE route_id = ? AND day_of_week = ?",
            (route_id, day_of_week),
        )


def insert_commute_samples(route_id: int, samples: Iterable[dict]) -> None:
    rows = [
        (
            route_id,
            s["day_of_week"],
            s["departure_time"],
            s["duration_minutes"],
        )
        for s in samples
    ]
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO commute_data
                (route_id, day_of_week, departure_time, duration_minutes)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )


def get_heatmap(route_id: int) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT day_of_week, departure_time, duration_minutes
            FROM commute_data
            WHERE route_id = ?
            ORDER BY day_of_week, departure_time
            """,
            (route_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def insert_observations(
    route_id: int, samples: Iterable[dict], source: str = "batch"
) -> None:
    """Append durations to the history table. ``source`` is 'batch' or 'live'."""
    rows = [
        (
            route_id,
            s["day_of_week"],
            s["departure_time"],
            s["duration_minutes"],
            source,
        )
        for s in samples
        if s.get("duration_minutes") is not None
    ]
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO observations
                (route_id, day_of_week, departure_time, duration_minutes, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )


def _effective_since(baseline_since: str | None) -> str:
    """Lower bound for trailing stats: the later of baseline_since and the
    rolling RECENT_DAYS window. Always returns a 'YYYY-MM-DD' string."""
    recent = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    if baseline_since and baseline_since > recent:
        return baseline_since
    return recent


def get_day_slot_stats(
    route_id: int, day_of_week: str, baseline_since: str | None = None
) -> dict[str, dict]:
    """Per-slot summary stats for one weekday, keyed by departure_time.

    Only observations on/after the effective baseline are considered. Slots
    with fewer than ``MIN_SAMPLES_FOR_STATS`` rows are still returned (callers
    decide whether the count is enough to trust).
    """
    since = _effective_since(baseline_since)
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT departure_time, duration_minutes
            FROM observations
            WHERE route_id = ? AND day_of_week = ? AND date(observed_at) >= ?
            """,
            (route_id, day_of_week, since),
        )
        by_slot: dict[str, list[float]] = {}
        for r in cur.fetchall():
            by_slot.setdefault(r["departure_time"], []).append(
                float(r["duration_minutes"])
            )
    return {slot: summarize(durs) for slot, durs in by_slot.items() if durs}


def get_day_data(route_id: int, day_of_week: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT departure_time, duration_minutes
            FROM commute_data
            WHERE route_id = ? AND day_of_week = ?
            ORDER BY departure_time
            """,
            (route_id, day_of_week),
        )
        return [dict(r) for r in cur.fetchall()]
