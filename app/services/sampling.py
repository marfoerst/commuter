import asyncio
import logging
from datetime import datetime, time, timedelta

import httpx

from app.config import CONCURRENT_REQUESTS
from app.db.models import (
    clear_day_data,
    clear_route_data,
    get_all_active_routes,
    insert_commute_samples,
    insert_observations,
)
from app.db.users import add_api_usage, get_api_usage_today, list_users
from app.config import USER_DAILY_API_BUDGET
from app.services.google_routes import compute_route_duration

log = logging.getLogger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_INDEX = {d: i for i, d in enumerate(WEEKDAYS)}


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def next_weekday_at(target_weekday: int, t: time, now: datetime | None = None) -> datetime:
    """Return the next datetime that falls on target_weekday at time t."""
    now = now or datetime.now().astimezone()
    today_weekday = now.weekday()
    days_ahead = (target_weekday - today_weekday) % 7
    candidate = now.replace(
        hour=t.hour, minute=t.minute, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def generate_time_slots(start: str, end: str, interval_minutes: int) -> list[time]:
    start_t = parse_hhmm(start)
    end_t = parse_hhmm(end)
    today = datetime.today().date()
    cur = datetime.combine(today, start_t)
    end_dt = datetime.combine(today, end_t)
    slots: list[time] = []
    while cur <= end_dt:
        slots.append(cur.time())
        cur += timedelta(minutes=interval_minutes)
    return slots


async def sample_route(route: dict, only_days: list[str] | None = None) -> list[dict]:
    weekdays = [w.strip() for w in route["weekdays"].split(",") if w.strip()]
    if only_days is not None:
        only = set(only_days)
        weekdays = [w for w in weekdays if w in only]
    slots = generate_time_slots(
        route["time_window_start"],
        route["time_window_end"],
        route["interval_minutes"],
    )
    sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        async def task(day_name: str, slot_time: time):
            if day_name not in WEEKDAY_INDEX:
                return
            async with sem:
                weekday_idx = WEEKDAY_INDEX[day_name]
                dep_dt = next_weekday_at(weekday_idx, slot_time)
                duration = await compute_route_duration(
                    client, route["origin"], route["destination"], dep_dt
                )
                if duration is not None:
                    results.append(
                        {
                            "day_of_week": day_name,
                            "departure_time": slot_time.strftime("%H:%M"),
                            "duration_minutes": duration,
                        }
                    )

        tasks = [task(d, s) for d in weekdays for s in slots]
        log.info(
            "Sampling %d combinations (%d days x %d slots, concurrency=%d)",
            len(tasks), len(weekdays), len(slots), CONCURRENT_REQUESTS,
        )
        await asyncio.gather(*tasks)

    return results


def _planned_calls(route: dict, only_days: list[str] | None) -> int:
    """How many Google Routes API calls a full sample of this route will make."""
    weekdays = [w.strip() for w in route["weekdays"].split(",") if w.strip()]
    if only_days is not None:
        only = set(only_days)
        weekdays = [w for w in weekdays if w in only]
    slots = generate_time_slots(
        route["time_window_start"], route["time_window_end"], route["interval_minutes"]
    )
    return len(weekdays) * len(slots)


async def recompute_user_routes(
    user_id: int, only_today: bool = False
) -> dict[str, int]:
    """Resample one user's active routes. Returns {route_name: sample_count}.

    only_today=False (manual /recompute): re-sample the full week and replace
    every route's data — used to seed/reset the heatmap.

    only_today=True (daily batch): re-sample just today's weekday column.

    Records Google Routes API calls against the user's daily budget and skips
    (best-effort) once the budget is exhausted, so the shared key stays bounded.
    """
    routes = get_all_active_routes(user_id)
    if not routes:
        return {}

    today_name: str | None = None
    only_days: list[str] | None = None
    if only_today:
        today_name = WEEKDAYS[datetime.now().astimezone().weekday()]
        only_days = [today_name]

    counts: dict[str, int] = {}
    for route in routes:
        if USER_DAILY_API_BUDGET:
            spent = get_api_usage_today(user_id)
            if spent + _planned_calls(route, only_days) > USER_DAILY_API_BUDGET:
                log.warning(
                    "User %s over daily API budget; skipping recompute of '%s'",
                    user_id, route["name"],
                )
                continue
        samples = await sample_route(route, only_days=only_days)
        add_api_usage(user_id, _planned_calls(route, only_days))
        if only_today:
            clear_day_data(route["id"], today_name)
        else:
            clear_route_data(route["id"])
        insert_commute_samples(route["id"], samples)
        # Append to the history table too; commute_data only keeps the latest
        # forecast per slot, observations accumulate it over time for stats.
        insert_observations(route["id"], samples, source="batch")
        counts[route["name"]] = len(samples)
        log.info(
            "Stored %d samples for user %s route '%s' (id=%s)%s",
            len(samples), user_id, route["name"], route["id"],
            f" [today only: {today_name}]" if only_today else "",
        )
    return counts


async def recompute_all_users(only_today: bool = False) -> dict[int, dict[str, int]]:
    """Recompute every user's routes (daily batch). Returns {user_id: counts}."""
    out: dict[int, dict[str, int]] = {}
    for user in list_users():
        counts = await recompute_user_routes(user["id"], only_today=only_today)
        if counts:
            out[user["id"]] = counts
    return out
