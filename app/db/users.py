"""Data-access for users, sessions, and per-user API usage.

Kept separate from ``models.py`` (which owns routes/observations) so the auth
layer stays a focused, independently testable unit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.config import SESSION_TTL_DAYS
from app.db.database import get_conn
from app.services.auth import hash_password, new_token

# Columns returned for a user (never expose password_hash to callers casually,
# but the dict carries it; routers must not serialize it).
_USER_COLS = (
    "id, username, password_hash, is_admin, api_token, "
    "ntfy_topic_url, webhook_url, push_min_severity, created_at"
)


# --- Users -------------------------------------------------------------------


def create_user(
    username: str,
    password: str,
    is_admin: bool = False,
) -> int:
    """Insert a user with a hashed password and a fresh API token. Raises
    sqlite3.IntegrityError if the username already exists."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, is_admin, api_token)
            VALUES (?, ?, ?, ?)
            """,
            (username, hash_password(password), 1 if is_admin else 0, new_token()),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLS} FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLS} FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_api_token(token: str) -> dict | None:
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLS} FROM users WHERE api_token = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_USER_COLS} FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]


def count_users() -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"])


def update_user_password(user_id: int, new_password: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        # Force re-login everywhere after a password change.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def update_user_push_settings(
    user_id: int,
    ntfy_topic_url: str | None,
    webhook_url: str | None,
    push_min_severity: str,
) -> None:
    sev = push_min_severity if push_min_severity in ("watch", "alert") else "alert"
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET ntfy_topic_url = ?, webhook_url = ?, push_min_severity = ?
            WHERE id = ?
            """,
            (ntfy_topic_url or None, webhook_url or None, sev, user_id),
        )


def regenerate_api_token(user_id: int) -> str:
    token = new_token()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET api_token = ? WHERE id = ?", (token, user_id)
        )
    return token


def delete_user(user_id: int) -> None:
    """Delete a user; ON DELETE CASCADE removes their routes, data, sessions."""
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- Sessions ----------------------------------------------------------------


def create_session(user_id: int) -> str:
    token = new_token()
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
    return token


def get_user_for_session(token: str) -> dict | None:
    """Return the user for a non-expired session token, else None."""
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT {', '.join('u.' + c.strip() for c in _USER_COLS.split(','))}
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now),
        ).fetchone()
        return dict(row) if row else None


def delete_session(token: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))


# --- API usage (per-user daily budget) ---------------------------------------


def add_api_usage(user_id: int | None, n: int) -> None:
    """Increment today's Google Routes API call count for a user."""
    if user_id is None or n <= 0:
        return
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO api_usage (user_id, day, count) VALUES (?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET count = count + excluded.count
            """,
            (user_id, today, n),
        )


def get_api_usage_today(user_id: int | None) -> int:
    if user_id is None:
        return 0
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM api_usage WHERE user_id = ? AND day = ?",
            (user_id, today),
        ).fetchone()
        return int(row["count"]) if row else 0
