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


def _route_weekdays(route: dict) -> list[str]:
    return [w.strip() for w in (route.get("weekdays") or "").split(",") if w.strip()]


def _is_active_day(route: dict, weekday_name: str) -> bool:
    """True if weekday_name is one of the route's configured commute days.

    Routes with no weekdays configured are treated as always-active (so we
    never silently stop answering for a misconfigured route).
    """
    wd = _route_weekdays(route)
    return (not wd) or (weekday_name in wd)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class DirectionConfig(BaseModel):
    time_window_start: str = "07:00"
    time_window_end: str = "09:00"
    interval_minutes: int = Field(default=15, ge=1, le=120)
    weekdays: str = "Mon,Tue,Wed,Thu,Fri"
    arrival_deadline: str | None = None  # HH:MM, optional; "be at the destination by this time"


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
        cfg.morning.arrival_deadline,
    )
    evening_id = upsert_named_route(
        "evening",
        cfg.destination,  # reversed
        cfg.origin,
        cfg.evening.time_window_start,
        cfg.evening.time_window_end,
        cfg.evening.interval_minutes,
        cfg.evening.weekdays,
        cfg.evening.arrival_deadline,
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


def _parse_hhmm_to_dt(s: str, base_date, tz) -> datetime:
    h, m = s.split(":")
    return datetime.combine(base_date, time(int(h), int(m))).replace(tzinfo=tz)


def _compute_candidates(
    day_data: list[dict],
    now: datetime,
    deadline_str: str | None,
) -> tuple[list[dict], datetime | None]:
    """Return (feasible_candidates, deadline_dt).

    Each candidate dict has: departure_time, duration_minutes, arrival_time,
    buffer_minutes (None if no deadline), departure_dt (internal use).
    Candidates are filtered to those still in the future and (if deadline is
    set) those that arrive on or before the deadline.

    Sorted:
      - deadline set      → by departure_time DESCENDING (latest safe first)
      - no deadline       → by duration ASCENDING (shortest drive first)
    """
    today_date = now.date()
    tz = now.tzinfo
    deadline_dt: datetime | None = None
    if deadline_str:
        try:
            deadline_dt = _parse_hhmm_to_dt(deadline_str, today_date, tz)
        except (ValueError, AttributeError):
            deadline_dt = None

    cutoff = now + timedelta(seconds=90)
    out: list[dict] = []
    for d in day_data:
        try:
            dep_dt = _parse_hhmm_to_dt(d["departure_time"], today_date, tz)
        except ValueError:
            continue
        if dep_dt <= cutoff:
            continue
        arrival_dt = dep_dt + timedelta(minutes=float(d["duration_minutes"]))
        if deadline_dt and arrival_dt > deadline_dt:
            continue
        buffer_min: int | None = None
        if deadline_dt:
            buffer_min = int(round((deadline_dt - arrival_dt).total_seconds() / 60))
        out.append(
            {
                "departure_time": d["departure_time"],
                "duration_minutes": round(float(d["duration_minutes"])),
                "arrival_time": arrival_dt.strftime("%H:%M"),
                "buffer_minutes": buffer_min,
                "_dep_dt": dep_dt,
            }
        )

    if deadline_dt:
        out.sort(key=lambda c: c["departure_time"], reverse=True)
    else:
        out.sort(key=lambda c: c["duration_minutes"])
    return out, deadline_dt


TOP_N_LIVE = 3
# Incident fallback: probe earlier slots in batches of TOP_N_LIVE. At 2 the
# worst case is 7 live calls per /today request (leave-now + 2 batches of 3);
# the normal no-incident case still costs only 4. This keeps graceful
# expand-on-failure for one extra batch while capping the cost — set to 1 to
# disable expansion entirely (max 4 calls) or 3 for the original behavior
# (up to 9 calls on heavy-incident mornings).
MAX_PROBE_BATCHES = 2
INCIDENT_ALERT_DELTA_MIN = 20
INCIDENT_WATCH_DELTA_MIN = 10
INCIDENT_ALERT_RATIO = 1.5


def _build_live_candidate(
    c: dict, live_dur: float | None, deadline_dt: datetime | None
) -> dict | None:
    """Convert a snapshot candidate + live duration into the return shape.

    Returns None if the live arrival violates the deadline (filtered out).
    """
    snapshot_dur = float(c["duration_minutes"])
    dur = float(live_dur) if live_dur is not None else snapshot_dur
    arrival = c["_dep_dt"] + timedelta(minutes=dur)
    if deadline_dt and arrival > deadline_dt:
        return None
    buffer_min = (
        int(round((deadline_dt - arrival).total_seconds() / 60))
        if deadline_dt
        else None
    )
    severity, delta = _classify_incident(live_dur, snapshot_dur)
    return {
        "departure_time": c["departure_time"],
        "duration_minutes": round(dur),
        "snapshot_duration_minutes": round(snapshot_dur),
        "arrival_time": arrival.strftime("%H:%M"),
        "buffer_minutes": buffer_min,
        "live": live_dur is not None,
        "delta_minutes": delta,
        "incident_severity": severity,
        "_dep_dt": c["_dep_dt"],
    }


def _classify_incident(live_dur: float | None, snapshot_dur: float) -> tuple[str, int]:
    """Compare live to snapshot. Returns (severity, delta_minutes).

    severity in {"clear", "watch", "alert"}; delta is round(live - snapshot).
    """
    if live_dur is None:
        return "clear", 0
    delta = int(round(float(live_dur) - float(snapshot_dur)))
    ratio = float(live_dur) / float(snapshot_dur) if snapshot_dur > 0 else 1.0
    if delta >= INCIDENT_ALERT_DELTA_MIN or ratio >= INCIDENT_ALERT_RATIO:
        return "alert", delta
    if delta >= INCIDENT_WATCH_DELTA_MIN:
        return "watch", delta
    return "clear", delta


def _incident_note(severity: str, delta: int) -> str:
    if severity == "alert":
        return f"Live drive is +{delta} min vs morning forecast. Likely incident on route."
    if severity == "watch":
        return f"Heavier traffic than forecast (+{delta} min)."
    return "Conditions normal."


async def _today_payload(client: httpx.AsyncClient, route: dict) -> dict:
    """Compute the live 'today' payload for a single named route.

    - Use the daily snapshot to find feasible candidate slots today.
    - Fire live Routes API calls in parallel for leave-now AND each of the
      top-N snapshot candidates. Re-rank by live duration (since real-time
      traffic / incidents can flip the ranking).
    - After re-rank, the top-1 becomes the recommended "best".
    - Alternatives returned in the payload use the live durations.
    """
    now = datetime.now().astimezone()
    today_weekday = WEEKDAYS[now.weekday()]
    deadline_str = route.get("arrival_deadline")

    # Don't spend Google Routes API calls on days this route doesn't run
    # (e.g. weekends). The snapshot has no data for these days anyway.
    if not _is_active_day(route, today_weekday):
        return {
            "name": route["name"],
            "day_of_week": today_weekday,
            "origin": route["origin"],
            "destination": route["destination"],
            "arrival_deadline": deadline_str,
            "best_departure_time": None,
            "optimal_duration": None,
            "arrival_time": None,
            "buffer_minutes": None,
            "current_duration": None,
            "time_savings": 0,
            "live": False,
            "alternatives": [],
            "incident_severity": "clear",
            "incident_delta_minutes": 0,
            "incident_note": "Conditions normal.",
            "note": "Not a configured commute day; skipping live lookup.",
        }

    day_data = get_day_data(route["id"], today_weekday)
    live_now_dt = now + timedelta(seconds=30)

    snapshot_candidates, deadline_dt = _compute_candidates(day_data, now, deadline_str)

    # First batch: top N (latest-safe or shortest-drive). Always fire leave-now
    # in parallel with this first batch.
    first_batch = snapshot_candidates[:TOP_N_LIVE]
    remaining = snapshot_candidates[TOP_N_LIVE:]
    tasks = [
        compute_route_duration(client, route["origin"], route["destination"], live_now_dt)
    ]
    tasks.extend(
        compute_route_duration(client, route["origin"], route["destination"], c["_dep_dt"])
        for c in first_batch
    )
    results = await asyncio.gather(*tasks)
    current_live = results[0]

    live_candidates: list[dict] = []
    probed: list[dict] = []
    for c, live_dur in zip(first_batch, results[1:]):
        probed.append({"_c": c, "_live": live_dur})
        built = _build_live_candidate(c, live_dur, deadline_dt)
        if built is not None:
            live_candidates.append(built)

    # Expand-on-failure: if the first batch yielded no feasible candidates and a
    # deadline is set, probe earlier batches (the snapshot order is latest-first
    # with a deadline, so "earlier" = later in the list). Cap at MAX_PROBE_BATCHES.
    probe_count = 1
    while (
        not live_candidates
        and deadline_dt is not None
        and remaining
        and probe_count < MAX_PROBE_BATCHES
    ):
        next_batch = remaining[:TOP_N_LIVE]
        remaining = remaining[TOP_N_LIVE:]
        durs = await asyncio.gather(
            *[
                compute_route_duration(client, route["origin"], route["destination"], c["_dep_dt"])
                for c in next_batch
            ]
        )
        for c, live_dur in zip(next_batch, durs):
            probed.append({"_c": c, "_live": live_dur})
            built = _build_live_candidate(c, live_dur, deadline_dt)
            if built is not None:
                live_candidates.append(built)
        probe_count += 1

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

    # Re-rank using live data: latest safe first (with deadline) or shortest
    # drive first (without).
    if deadline_dt:
        live_candidates.sort(key=lambda c: c["departure_time"], reverse=True)
    else:
        live_candidates.sort(key=lambda c: c["duration_minutes"])
    # Trim to top 3 for return.
    live_candidates = live_candidates[:TOP_N_LIVE]

    payload: dict = {
        "name": route["name"],
        "day_of_week": today_weekday,
        "origin": route["origin"],
        "destination": route["destination"],
        "current_duration": round(current_live),
        "arrival_deadline": deadline_str,
        "live": True,
    }

    if not live_candidates:
        reason = (
            "Every remaining slot arrives after the deadline (live)."
            if deadline_dt
            else "No recommended departure remaining today."
        )
        arrival_dt = now + timedelta(minutes=current_live)
        buffer_min: int | None = None
        if deadline_dt:
            buffer_min = int(round((deadline_dt - arrival_dt).total_seconds() / 60))
        payload.update(
            {
                "best_departure_time": now.strftime("%H:%M"),
                "optimal_duration": round(current_live),
                "arrival_time": arrival_dt.strftime("%H:%M"),
                "buffer_minutes": buffer_min,
                "time_savings": 0,
                "alternatives": [],
                "incident_severity": "clear",
                "incident_delta_minutes": 0,
                "incident_note": "Conditions normal.",
                "note": reason,
            }
        )
        return payload

    best = live_candidates[0]
    alt_out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in live_candidates]

    # Worst incident signal across all top candidates — alert beats watch beats clear.
    rank = {"alert": 2, "watch": 1, "clear": 0}
    worst = max(live_candidates, key=lambda c: rank[c["incident_severity"]])
    worst_sev = worst["incident_severity"]
    worst_delta = worst["delta_minutes"]

    payload.update(
        {
            "best_departure_time": best["departure_time"],
            "optimal_duration": best["duration_minutes"],
            "arrival_time": best["arrival_time"],
            "buffer_minutes": best["buffer_minutes"],
            "time_savings": round(current_live - best["duration_minutes"]),
            "alternatives": alt_out,
            "incident_severity": worst_sev,
            "incident_delta_minutes": worst_delta,
            "incident_note": _incident_note(worst_sev, worst_delta),
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


@router.get("/commute/today/{direction}/next")
async def commute_today_next(
    direction: str,
    minutes: int = 60,
    _: None = Depends(require_api_key),
):
    """Best departure(s) within the next N minutes from now.

    Filters today's snapshot candidates to those whose departure_time falls
    between (now+30s) and (now+N min). Live-rechecks the top 3 of those via
    Google Routes API. Honors the route's arrival_deadline.

    Designed for "what should I do in the immediate window" dashboard tiles
    (e.g. on a phone homescreen) — independent of the full-day best slot.
    """
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(400, f"Invalid direction. Use one of {VALID_DIRECTIONS}.")
    if minutes < 1 or minutes > 360:
        raise HTTPException(400, "minutes must be in [1, 360]")

    route = get_route_by_name(direction)
    if not route:
        raise HTTPException(404, f"No active '{direction}' route configured")

    now = datetime.now().astimezone()
    today_weekday = WEEKDAYS[now.weekday()]
    day_data = get_day_data(route["id"], today_weekday)
    cutoff_end = now + timedelta(minutes=minutes)
    deadline_str = route.get("arrival_deadline")

    snapshot_candidates, deadline_dt = _compute_candidates(day_data, now, deadline_str)
    in_window = [c for c in snapshot_candidates if c["_dep_dt"] <= cutoff_end]

    base = {
        "name": route["name"],
        "day_of_week": today_weekday,
        "origin": route["origin"],
        "destination": route["destination"],
        "window_minutes": minutes,
        "window_end_time": cutoff_end.strftime("%H:%M"),
        "arrival_deadline": deadline_str,
        "live": True,
    }

    if not _is_active_day(route, today_weekday):
        return {
            **base,
            "live": False,
            "best": None,
            "candidates": [],
            "note": "Not a configured commute day; skipping live lookup.",
        }

    if not in_window:
        return {
            **base,
            "best": None,
            "candidates": [],
            "note": f"No feasible departure in the next {minutes} min.",
        }

    out: list[dict] = []
    remaining = list(in_window)
    async with httpx.AsyncClient() as client:
        probe_count = 0
        while remaining and probe_count < MAX_PROBE_BATCHES:
            batch = remaining[:TOP_N_LIVE]
            remaining = remaining[TOP_N_LIVE:]
            durs = await asyncio.gather(
                *[
                    compute_route_duration(client, route["origin"], route["destination"], c["_dep_dt"])
                    for c in batch
                ]
            )
            for c, live_dur in zip(batch, durs):
                built = _build_live_candidate(c, live_dur, deadline_dt)
                if built is not None:
                    out.append(built)
            probe_count += 1
            # Stop if we already have feasible options and a deadline is set.
            # Without a deadline, first batch's shortest-drive is already the answer.
            if out:
                break

    # Strip internal _dep_dt
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in out]

    if deadline_dt:
        out.sort(key=lambda c: c["departure_time"], reverse=True)
    else:
        out.sort(key=lambda c: c["duration_minutes"])
    out = out[:TOP_N_LIVE]

    best = out[0] if out else None
    return {**base, "best": best, "candidates": out}


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
