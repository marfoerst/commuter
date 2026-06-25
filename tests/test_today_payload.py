"""Offline integration test for the live re-rank payload.

Monkeypatches the two Google Routes API calls so the whole `_today_payload`
path runs without network, and asserts the new fields wire through end to end.
"""

from datetime import datetime

import pytest

from app.api import routes as routes_mod
from app.db.database import get_conn, init_db
from app.db.models import (
    get_route_by_name,
    insert_commute_samples,
    insert_observations,
    set_route_bonn_segments,
    upsert_named_route,
)
from app.services.sampling import WEEKDAYS

# Slots every 30 min across the whole day so at least one is in the future
# regardless of when the test runs.
ALL_DAY_SLOTS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]


@pytest.fixture
def seeded(monkeypatch):
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM commute_data")
        conn.execute("DELETE FROM routes")

    upsert_named_route(
        "morning", "Home", "Office", "00:00", "23:30", 30, ",".join(WEEKDAYS)
    )
    route = get_route_by_name("morning")
    today = WEEKDAYS[datetime.now().astimezone().weekday()]

    # Snapshot: a flat ~30-min forecast for every slot today.
    samples = [
        {"day_of_week": today, "departure_time": s, "duration_minutes": 30.0}
        for s in ALL_DAY_SLOTS
    ]
    insert_commute_samples(route["id"], samples)
    # History: enough observations per slot that "typical" is trusted (~30 min).
    for _ in range(5):
        insert_observations(route["id"], samples, source="batch")

    # Live drive comes back at 60 min everywhere → +30 vs the 30-min typical.
    async def fake_duration(client, origin, dest, dep_dt):
        return 60.0

    async def fake_alternatives(client, origin, dest, dep_dt, max_alternatives=3):
        return [
            {"label": "A565", "duration_minutes": 58, "distance_km": 12.0},
            {"label": "B56 via centre", "duration_minutes": 66, "distance_km": 10.0},
        ]

    monkeypatch.setattr(routes_mod, "compute_route_duration", fake_duration)
    monkeypatch.setattr(routes_mod, "compute_route_alternatives", fake_alternatives)
    return route


async def _run(route):
    import httpx

    async with httpx.AsyncClient() as client:
        return await routes_mod._today_payload(client, route)


def test_payload_has_new_fields_and_typical_incident(seeded):
    import asyncio

    now = datetime.now().astimezone()
    if now.hour == 23 and now.minute >= 30:
        pytest.skip("no future slot this late in the day")

    payload = asyncio.run(_run(seeded))

    # Route options surfaced, fastest-first.
    assert payload["route_options"][0]["label"] == "A565"
    assert payload["route_options"][0]["duration_minutes"] == 58

    # Reliability fields present (history existed).
    assert payload["typical_duration"] == 30
    assert payload["p90_duration"] is not None

    # Incident judged vs *typical* (30), not the snapshot, and 60 vs 30 = +30.
    assert payload["incident_severity"] == "alert"
    assert payload["incident_delta_minutes"] == 30
    assert "typical for this time" in payload["incident_note"]


def test_live_probes_recorded_to_history(seeded):
    import asyncio

    now = datetime.now().astimezone()
    if now.hour == 23 and now.minute >= 30:
        pytest.skip("no future slot this late in the day")

    with get_conn() as conn:
        before = conn.execute(
            "SELECT COUNT(*) c FROM observations WHERE source='live'"
        ).fetchone()["c"]

    asyncio.run(_run(seeded))

    with get_conn() as conn:
        after = conn.execute(
            "SELECT COUNT(*) c FROM observations WHERE source='live'"
        ).fetchone()["c"]
    assert after > before


def test_local_traffic_drives_incident_when_google_clear(monkeypatch):
    """A Bonn 'Staugefahr' segment elevates incident_severity even when the live
    Google probe matches typical (no Google-side incident)."""
    import asyncio

    now = datetime.now().astimezone()
    if now.hour == 23 and now.minute >= 30:
        pytest.skip("no future slot this late in the day")

    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM commute_data")
        conn.execute("DELETE FROM routes")

    upsert_named_route(
        "morning", "Home", "Office", "00:00", "23:30", 30, ",".join(WEEKDAYS)
    )
    route = get_route_by_name("morning")
    today = WEEKDAYS[now.weekday()]
    samples = [
        {"day_of_week": today, "departure_time": s, "duration_minutes": 30.0}
        for s in ALL_DAY_SLOTS
    ]
    insert_commute_samples(route["id"], samples)
    for _ in range(5):
        insert_observations(route["id"], samples, source="batch")
    # This route's matched Bonn segment.
    set_route_bonn_segments(route["id"], [42])
    route = get_route_by_name("morning")  # reload with bonn_segment_ids

    # Google clear: live == typical (30) → no Google-side incident.
    async def fake_duration(client, origin, dest, dep_dt):
        return 30.0

    async def fake_alternatives(client, origin, dest, dep_dt, max_alternatives=3):
        return []

    # Bonn feed reports a jam on the matched segment.
    async def fake_fetch_traffic(client):
        return [
            {
                "geometry": {"type": "MultiLineString", "coordinates": [[[7.1, 50.7]]]},
                "properties": {
                    "strecke_id": 42,
                    "auswertezeit": "2026-06-25T08:00:00Z",
                    "geschwindigkeit": 6,
                    "verkehrsstatus": "Staugefahr",
                },
            }
        ]

    monkeypatch.setattr(routes_mod, "compute_route_duration", fake_duration)
    monkeypatch.setattr(routes_mod, "compute_route_alternatives", fake_alternatives)
    monkeypatch.setattr(routes_mod, "fetch_traffic", fake_fetch_traffic)

    payload = asyncio.run(_run(route))

    assert payload["local_traffic"]["worst_status"] == "Staugefahr"
    assert payload["local_traffic"]["min_speed_kmh"] == 6
    assert payload["incident_severity"] == "alert"
    assert "Bonn live traffic" in payload["incident_note"]
