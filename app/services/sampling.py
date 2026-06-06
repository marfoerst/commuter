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
)
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


async def recompute_all_active_routes(only_today: bool = False) -> dict[str, int]:
    """Resample active routes. Returns {route_name: sample_count}.

    only_today=False (manual /recompute): re-sample the full week and replace
    every route's data — used to seed/reset the heatmap.

    only_today=True (daily batch): re-sample just today's weekday column and
    replace only that day, leaving the rest of the week intact. This avoids
    re-forecasting the whole week every morning (the dominant source of Google
    Routes API calls) while keeping each day's forecast fresh on its own day.
    """
    routes = get_all_active_routes()
    if not routes:
        log.info("No active routes configured; skipping recompute")
        return {}

    today_name: str | None = None
    only_days: list[str] | None = None
    if only_today:
        today_name = WEEKDAYS[datetime.now().astimezone().weekday()]
        only_days = [today_name]

    counts: dict[str, int] = {}
    for route in routes:
        samples = await sample_route(route, only_days=only_days)
        if only_today:
            clear_day_data(route["id"], today_name)
        else:
            clear_route_data(route["id"])
        insert_commute_samples(route["id"], samples)
        counts[route["name"]] = len(samples)
        log.info(
            "Stored %d samples for route '%s' (id=%s)%s",
            len(samples), route["name"], route["id"],
            f" [today only: {today_name}]" if only_today else "",
        )
    return counts
