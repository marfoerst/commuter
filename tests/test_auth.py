"""Auth unit + endpoint tests (offline, via Starlette TestClient)."""

import pytest
from fastapi.testclient import TestClient

from app.db.database import get_conn, init_db
from app.services.auth import hash_password, new_token, verify_password


def test_hash_roundtrip_and_reject():
    h = hash_password("hunter2")
    assert h.startswith("pbkdf2$")
    assert verify_password("hunter2", h)
    assert not verify_password("wrong", h)
    # Two hashes of the same password differ (random salt).
    assert hash_password("hunter2") != h


def test_verify_rejects_garbage():
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")


def test_new_token_unique():
    assert new_token() != new_token()


@pytest.fixture
def client():
    init_db()
    with get_conn() as conn:
        for t in ("sessions", "api_usage", "observations", "commute_data", "routes", "users"):
            conn.execute(f"DELETE FROM {t}")
    from app.db.users import create_user

    create_user("admin", "adminpass", is_admin=True)
    create_user("bob", "bobpass", is_admin=False)
    # TestClient without the lifespan (we seeded users ourselves).
    from app.main import app

    return TestClient(app)


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def test_requires_auth(client):
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_login_logout_flow(client):
    assert _login(client, "admin", "wrong").status_code == 401
    r = _login(client, "admin", "adminpass")
    assert r.status_code == 200 and r.json()["user"]["is_admin"] == 1
    assert client.get("/api/me").json()["user"]["username"] == "admin"
    # password hash never leaks
    assert "password_hash" not in r.json()["user"]
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_api_token_auth(client):
    _login(client, "bob", "bobpass")
    token = client.post("/api/me/api-token").json()["api_token"]
    client.post("/api/logout")
    # Cookie gone, but the token authenticates as bob.
    assert client.get("/api/config", headers={"X-API-Key": token}).status_code == 200
    assert client.get("/api/config", headers={"X-API-Key": "bogus"}).status_code == 401


def test_password_change_invalidates_sessions(client):
    _login(client, "bob", "bobpass")
    assert client.post("/api/me/password", json={"password": "newpass1"}).status_code == 200
    # Old session was cleared server-side → now unauthenticated.
    assert client.get("/api/me").status_code == 401
    assert _login(client, "bob", "newpass1").status_code == 200
