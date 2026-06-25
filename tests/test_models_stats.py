from datetime import date, timedelta

import pytest

from app.db.database import get_conn, init_db
from app.db.models import get_day_slot_stats, get_route_by_name, upsert_named_route
from app.db.users import create_user


@pytest.fixture
def route_id():
    init_db()
    # clean slate for repeatable assertions
    with get_conn() as conn:
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM routes")
        conn.execute("DELETE FROM users")
    uid = create_user("tester", "pw")
    upsert_named_route(
        uid, "morning", "A", "B", "07:00", "09:00", 30, "Mon,Tue,Wed,Thu,Fri"
    )
    return get_route_by_name(uid, "morning")["id"]


def _add(rid, slot, dur, days_ago):
    when = (date.today() - timedelta(days=days_ago)).isoformat() + " 08:00:00"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO observations (route_id, day_of_week, departure_time, "
            "duration_minutes, source, observed_at) VALUES (?,?,?,?,?,?)",
            (rid, "Mon", slot, dur, "live", when),
        )


def test_rolling_window_excludes_old(route_id):
    # 4 recent + 2 ancient (beyond the 35-day rolling window) for the same slot
    for d in (30, 31, 32, 60):
        _add(route_id, "08:00", d, days_ago=1)
    _add(route_id, "08:00", 999, days_ago=40)
    _add(route_id, "08:00", 999, days_ago=50)

    stats = get_day_slot_stats(route_id, "Mon", baseline_since=None)
    assert "08:00" in stats
    s = stats["08:00"]
    assert s["count"] == 4  # ancient ones dropped
    assert s["max_minutes"] == 60.0  # the 999s were excluded


def test_baseline_since_further_restricts(route_id):
    _add(route_id, "08:00", 30, days_ago=20)
    _add(route_id, "08:00", 32, days_ago=18)
    _add(route_id, "08:00", 80, days_ago=2)  # post-event spike

    cutoff = (date.today() - timedelta(days=10)).isoformat()
    stats = get_day_slot_stats(route_id, "Mon", baseline_since=cutoff)
    s = stats["08:00"]
    # only the post-cutoff observation survives
    assert s["count"] == 1
    assert s["typical_minutes"] == 80.0
