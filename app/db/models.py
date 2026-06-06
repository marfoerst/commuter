from typing import Iterable

from app.db.database import get_conn


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
) -> int:
    """Deactivate any previous row with this name, insert a new active one."""
    with get_conn() as conn:
        conn.execute("UPDATE routes SET is_active = 0 WHERE name = ?", (name,))
        cur = conn.execute(
            """
            INSERT INTO routes
                (name, origin, destination, time_window_start, time_window_end,
                 interval_minutes, weekdays, arrival_deadline, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
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
