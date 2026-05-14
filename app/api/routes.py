import asyncio
import logging
from datetime import datetime, time, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import API_KEY
from app.db.models import (
    get_all_active_routes,
    get_day_data,
    get_heatmap,
    get_route_by_name,
    upsert_named_route,
)
from app.services.google_routes import compute_route_duration
from app.services.sampling import WEEKDAYS, recompute_all_active_routes

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

VALID_DIRECTIONS = {"morning", "evening"}


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class DirectionConfig(BaseModel):
    time_window_start: str = "07:00"
    time_window_end: str = "09:00"
    interval_minutes: int = Field(default=10, ge=1, le=120)
    weekdays: str = "Mon,Tue,Wed,Thu,Fri"


class FullConfig(BaseModel):
    """Single home/office pair + per-direction sampling windows.

    Evening route is automatically stored with origin/destination reversed.
    """

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    morning: DirectionConfig = Field(default_factory=DirectionConfig)
    evening: DirectionConfig = Field(
        default_factory=lambda: DirectionConfig(
            time_window_start="16:00",
            time_window_end="18:30",
        )
    )


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/config")
async def get_config(_: None = Depends(require_api_key)):
    return {
        "morning": get_route_by_name("morning"),
        "evening": get_route_by_name("evening"),
    }


@router.post("/config")
async def set_config(cfg: FullConfig, _: None = Depends(require_api_key)):
    morning_id = upsert_named_route(
        "morning",
        cfg.origin,
        cfg.destination,
        cfg.morning.time_window_start,
        cfg.morning.time_window_end,
        cfg.morning.interval_minutes,
        cfg.morning.weekdays,
    )
    evening_id = upsert_named_route(
        "evening",
        cfg.destination,  # reversed
        cfg.origin,
        cfg.evening.time_window_start,
        cfg.evening.time_window_end,
        cfg.evening.interval_minutes,
        cfg.evening.weekdays,
    )
    return {
        "status": "ok",
        "morning_id": morning_id,
        "evening_id": evening_id,
    }


@router.post("/recompute")
async def recompute(_: None = Depends(require_api_key)):
    routes = get_all_active_routes()
    if not routes:
        raise HTTPException(400, "No active route configured")
    counts = await recompute_all_active_routes()
    return {"status": "ok", "samples": counts}


# ---------------------------------------------------------------------------
# Today (live) helpers
# ---------------------------------------------------------------------------


def _pick_current_slot(data: list[dict], now: datetime) -> dict:
    sorted_data = sorted(data, key=lambda d: d["departure_time"])
    now_minutes = now.hour * 60 + now.minute
    current = sorted_data[0]
    for d in sorted_data:
        h, m = d["departure_time"].split(":")
        slot_min = int(h) * 60 + int(m)
        if slot_min <= now_minutes:
            current = d
        else:
            break
    return current


async def _today_payload(client: httpx.AsyncClient, route: dict) -> dict:
    """Compute the live 'today' payload for a single named route."""
    now = datetime.now().astimezone()
    today_weekday = WEEKDAYS[now.weekday()]
    day_data = get_day_data(route["id"], today_weekday)
    live_now_dt = now + timedelta(seconds=30)

    # Find best remaining slot today from the daily snapshot.
    best_slot: dict | None = None
    best_dt: datetime | None = None
    if day_data:
        today_date = now.date()
        candidates: list[tuple[datetime, dict]] = []
        for d in day_data:
            h, m = d["departure_time"].split(":")
            slot_dt = datetime.combine(
                today_date, time(int(h), int(m))
            ).replace(tzinfo=now.tzinfo)
            if slot_dt > live_now_dt + timedelta(minutes=1):
                candidates.append((slot_dt, d))
        if candidates:
            best_dt, best_slot = min(
                candidates, key=lambda x: x[1]["duration_minutes"]
            )

    tasks = [
        compute_route_duration(
            client, route["origin"], route["destination"], live_now_dt
        )
    ]
    if best_dt is not None:
        tasks.append(
            compute_route_duration(
                client, route["origin"], route["destination"], best_dt
            )
        )
    results = await asyncio.gather(*tasks)
    current_live = results[0]
    optimal_live = results[1] if len(results) > 1 else None

    if current_live is None and day_data:
        fallback = _pick_current_slot(day_data, now.replace(tzinfo=None))
        current_live = fallback["duration_minutes"]
    if current_live is None:
        return {
            "name": route["name"],
            "error": "Routes API unavailable and no historical data",
            "origin": route["origin"],
            "destination": route["destination"],
        }

    payload = {
        "name": route["name"],
        "day_of_week": today_weekday,
        "origin": route["origin"],
        "destination": route["destination"],
        "current_duration": round(current_live),
        "live": True,
    }
    if best_slot is None:
        payload.update(
            {
                "best_departure_time": now.strftime("%H:%M"),
                "optimal_duration": round(current_live),
                "time_savings": 0,
                "note": "No recommended departure remaining today.",
            }
        )
    else:
        if optimal_live is None:
            optimal_live = best_slot["duration_minutes"]
        payload.update(
            {
                "best_departure_time": best_slot["departure_time"],
                "optimal_duration": round(optimal_live),
                "time_savings": round(current_live - optimal_live),
            }
        )
    return payload


# ---------------------------------------------------------------------------
# Today endpoints
# ---------------------------------------------------------------------------


@router.get("/commute/today")
async def commute_today_all(_: None = Depends(require_api_key)):
    routes = {r["name"]: r for r in get_all_active_routes()}
    if not routes:
        raise HTTPException(404, "No active route configured")
    async with httpx.AsyncClient() as client:
        result: dict[str, dict] = {}
        for name in ("morning", "evening"):
            if name in routes:
                result[name] = await _today_payload(client, routes[name])
    return result


@router.get("/commute/today/{direction}")
async def commute_today_direction(
    direction: str, _: None = Depends(require_api_key)
):
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(400, f"Invalid direction. Use one of {VALID_DIRECTIONS}.")
    route = get_route_by_name(direction)
    if not route:
        raise HTTPException(404, f"No active '{direction}' route configured")
    async with httpx.AsyncClient() as client:
        return await _today_payload(client, route)


# ---------------------------------------------------------------------------
# Heatmap endpoints
# ---------------------------------------------------------------------------


def _heatmap_payload(route: dict) -> list[dict]:
    return [
        {
            "day": d["day_of_week"],
            "time": d["departure_time"],
            "duration": d["duration_minutes"],
        }
        for d in get_heatmap(route["id"])
    ]


@router.get("/commute/heatmap")
async def commute_heatmap_all(_: None = Depends(require_api_key)):
    routes = {r["name"]: r for r in get_all_active_routes()}
    if not routes:
        raise HTTPException(404, "No active route configured")
    return {
        name: _heatmap_payload(routes[name])
        for name in ("morning", "evening")
        if name in routes
    }


@router.get("/commute/heatmap/{direction}")
async def commute_heatmap_direction(
    direction: str, _: None = Depends(require_api_key)
):
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(400, f"Invalid direction. Use one of {VALID_DIRECTIONS}.")
    route = get_route_by_name(direction)
    if not route:
        raise HTTPException(404, f"No active '{direction}' route configured")
    return _heatmap_payload(route)
