"""Multi-user isolation, admin management, and budget tests (offline)."""

import pytest
from fastapi.testclient import TestClient

from app.api import routes as routes_mod
from app.db.database import get_conn, init_db
from app.db.users import create_user


@pytest.fixture
def client(monkeypatch):
    init_db()
    with get_conn() as conn:
        for t in ("sessions", "api_usage", "observations", "commute_data", "routes", "users"):
            conn.execute(f"DELETE FROM {t}")
    create_user("admin", "adminpass", is_admin=True)
    create_user("alice", "alicepass")
    create_user("bob", "bobpass")

    # No network: stub the Bonn refresh so set_config is offline.
    async def _noop(_user_id):
        return None

    monkeypatch.setattr(routes_mod, "_refresh_bonn_segments", _noop)

    from app.main import app

    return TestClient(app)


def _login(client, u, p):
    return client.post("/api/login", json={"username": u, "password": p})


def test_route_isolation_between_users(client):
    _login(client, "alice", "alicepass")
    client.post("/api/config", json={"origin": "AliceHome", "destination": "AliceWork"})
    assert client.get("/api/config").json()["morning"]["origin"] == "AliceHome"
    client.post("/api/logout")

    # Bob sees nothing of Alice's.
    _login(client, "bob", "bobpass")
    assert client.get("/api/config").json() == {"morning": None, "evening": None}
    client.post("/api/config", json={"origin": "BobHome", "destination": "BobWork"})
    assert client.get("/api/config").json()["morning"]["origin"] == "BobHome"
    client.post("/api/logout")

    # Alice's data is unchanged.
    _login(client, "alice", "alicepass")
    assert client.get("/api/config").json()["morning"]["origin"] == "AliceHome"


def test_admin_can_manage_users_nonadmin_cannot(client):
    # Non-admin is forbidden.
    _login(client, "bob", "bobpass")
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/users", json={"username": "x", "password": "secret1"}).status_code == 403
    client.post("/api/logout")

    # Admin can create, and duplicate is rejected.
    _login(client, "admin", "adminpass")
    assert client.post("/api/admin/users", json={"username": "carol", "password": "secret1"}).status_code == 200
    assert client.post("/api/admin/users", json={"username": "carol", "password": "secret1"}).status_code == 409
    names = {u["username"] for u in client.get("/api/admin/users").json()["users"]}
    assert {"admin", "alice", "bob", "carol"} <= names

    # Admin can delete carol but not themselves.
    carol_id = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["username"] == "carol")
    admin_id = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["username"] == "admin")
    assert client.delete(f"/api/admin/users/{admin_id}").status_code == 400
    assert client.delete(f"/api/admin/users/{carol_id}").status_code == 200


def test_deleting_user_cascades_routes(client):
    _login(client, "alice", "alicepass")
    client.post("/api/config", json={"origin": "H", "destination": "W"})
    client.post("/api/logout")

    _login(client, "admin", "adminpass")
    alice_id = next(u["id"] for u in client.get("/api/admin/users").json()["users"] if u["username"] == "alice")
    client.delete(f"/api/admin/users/{alice_id}")
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM routes WHERE user_id = ?", (alice_id,)).fetchone()["c"]
    assert n == 0


def test_budget_forces_snapshot_only(client, monkeypatch):
    from datetime import datetime

    now = datetime.now().astimezone()
    if now.hour == 23 and now.minute >= 30:
        pytest.skip("no future slot this late in the day")

    from app.db.users import add_api_usage, get_user_by_username
    from app.db.models import get_route_by_name, insert_commute_samples
    from app.services.sampling import WEEKDAYS

    # Seed alice a route + a snapshot, then exhaust her budget.
    _login(client, "alice", "alicepass")
    client.post("/api/config", json={"origin": "H", "destination": "W"})
    alice = get_user_by_username("alice")
    route = get_route_by_name(alice["id"], "morning")
    today = WEEKDAYS[now.weekday()]
    slots = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
    insert_commute_samples(
        route["id"],
        [{"day_of_week": today, "departure_time": s, "duration_minutes": 25.0} for s in slots],
    )

    # Live call should NOT fire when over budget — make it explode if called.
    async def boom(*a, **k):
        raise AssertionError("Google should not be called when over budget")

    monkeypatch.setattr(routes_mod, "compute_route_duration", boom)
    monkeypatch.setattr(routes_mod, "compute_route_alternatives", boom)

    add_api_usage(alice["id"], 10_000)  # way over the default cap

    payload = client.get("/api/commute/today/morning").json()
    assert payload["live"] is False
    # Falls back to the snapshot duration rather than erroring.
    assert payload["current_duration"] == 25
