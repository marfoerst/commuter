"""Authentication, self-service, and admin user-management endpoints."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    SESSION_COOKIE,
    require_admin,
    require_user,
    sanitize_user,
)
from app.config import COOKIE_SECURE, SESSION_TTL_DAYS
from app.db.users import (
    create_session,
    create_user,
    delete_session,
    delete_user,
    get_user_by_id,
    get_user_by_username,
    list_users,
    regenerate_api_token,
    update_user_password,
    update_user_push_settings,
)
from app.services.auth import verify_password

router = APIRouter(prefix="/api")


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class PasswordBody(BaseModel):
    password: str = Field(min_length=6)


class NotificationsBody(BaseModel):
    ntfy_topic_url: str | None = None
    webhook_url: str | None = None
    push_min_severity: str = "alert"


class NewUserBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=6)
    is_admin: bool = False


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


# --- Auth --------------------------------------------------------------------


@router.post("/login")
async def login(body: LoginBody, response: Response):
    user = get_user_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session(user["id"])
    _set_session_cookie(response, token)
    return {"status": "ok", "user": sanitize_user(user)}


@router.post("/logout")
async def logout(request: Request, response: Response, user: dict = Depends(require_user)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return {"user": sanitize_user(user)}


@router.post("/me/password")
async def change_password(body: PasswordBody, user: dict = Depends(require_user)):
    update_user_password(user["id"], body.password)
    return {"status": "ok", "note": "Password changed; please log in again."}


@router.put("/me/notifications")
async def set_notifications(body: NotificationsBody, user: dict = Depends(require_user)):
    update_user_push_settings(
        user["id"], body.ntfy_topic_url, body.webhook_url, body.push_min_severity
    )
    return {"status": "ok"}


@router.post("/me/api-token")
async def rotate_api_token(user: dict = Depends(require_user)):
    token = regenerate_api_token(user["id"])
    return {"status": "ok", "api_token": token}


# --- Admin: user management --------------------------------------------------


@router.get("/admin/users")
async def admin_list_users(_: dict = Depends(require_admin)):
    return {"users": [sanitize_user(u) for u in list_users()]}


@router.post("/admin/users")
async def admin_create_user(body: NewUserBody, _: dict = Depends(require_admin)):
    try:
        uid = create_user(body.username, body.password, is_admin=body.is_admin)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already exists")
    return {"status": "ok", "user": sanitize_user(get_user_by_id(uid))}


@router.post("/admin/users/{user_id}/password")
async def admin_reset_password(
    user_id: int, body: PasswordBody, _: dict = Depends(require_admin)
):
    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    update_user_password(user_id, body.password)
    return {"status": "ok"}


@router.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Refusing to delete your own account")
    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    delete_user(user_id)
    return {"status": "ok"}
