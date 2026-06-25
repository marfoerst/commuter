"""FastAPI auth dependencies.

A request is authenticated by either a ``session`` cookie (browser login) or an
``X-API-Key`` header matching a user's ``api_token`` (programmatic / widgets /
Home Assistant). Both resolve to the same per-user identity.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.db.users import get_user_by_api_token, get_user_for_session

SESSION_COOKIE = "session"


def get_current_user(request: Request) -> dict | None:
    """Resolve the request's user from session cookie or API token, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    user = get_user_for_session(token) if token else None
    if user is None:
        api_key = request.headers.get("x-api-key")
        if api_key:
            user = get_user_by_api_token(api_key)
    return user


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def sanitize_user(user: dict) -> dict:
    """Strip the password hash before returning a user in an API response."""
    return {k: v for k, v in user.items() if k != "password_hash"}
