import logging
from datetime import date, datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import (
    PUSH_CHECK_MINUTES,
    SCHEDULER_HOUR,
    SCHEDULER_MINUTE,
)
from app.services.notify import (
    SEVERITY_RANK,
    meets_threshold,
    push_enabled,
    send_push,
)
from app.services.sampling import recompute_all_active_routes

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# route_name -> (date_iso, highest severity rank already pushed today). Used to
# avoid re-notifying for the same (or a lower) severity within a single day.
_last_pushed: dict[str, tuple[str, int]] = {}


async def daily_job() -> None:
    try:
        # Refresh only today's forecast column; the rest of the week's snapshot
        # stays put. Cuts the daily batch's Google Routes API calls ~5x vs.
        # re-sampling the whole week every morning.
        counts = await recompute_all_active_routes(only_today=True)
        log.info("Daily recompute complete (today only): %s", counts)
    except Exception:
        log.exception("Daily recompute failed")


def _within_window(route: dict, now: datetime) -> bool:
    """True if ``now`` falls inside the route's sampling window today."""
    try:
        sh, sm = (int(x) for x in route["time_window_start"].split(":"))
        eh, em = (int(x) for x in route["time_window_end"].split(":"))
    except (ValueError, KeyError):
        return False
    cur = now.hour * 60 + now.minute
    return sh * 60 + sm <= cur <= eh * 60 + em


def _should_push(route_name: str, severity: str, today: str) -> bool:
    """Push only the first time today the severity crosses the threshold, and
    again only if it escalates (watch -> alert). Resets each day."""
    if not meets_threshold(severity):
        return False
    rank = SEVERITY_RANK.get(severity, 0)
    last_day, last_rank = _last_pushed.get(route_name, ("", -1))
    if last_day != today:
        last_rank = -1
    if rank <= last_rank:
        return False
    _last_pushed[route_name] = (today, rank)
    return True


async def push_check_job() -> None:
    """Periodic in-window check: notify when live conditions are bad.

    Skips immediately outside commute windows / on non-commute days so it spends
    Google Routes API calls only when it could actually have something to say.
    """
    if not push_enabled():
        return
    # Imported lazily to keep the scheduler module free of the API layer at
    # import time.
    from app.api.routes import _is_active_day, _today_payload
    from app.db.models import get_all_active_routes
    from app.services.sampling import WEEKDAYS

    now = datetime.now().astimezone()
    today_iso = date.today().isoformat()
    weekday = WEEKDAYS[now.weekday()]

    routes = [
        r
        for r in get_all_active_routes()
        if _is_active_day(r, weekday) and _within_window(r, now)
    ]
    if not routes:
        return

    async with httpx.AsyncClient() as client:
        for route in routes:
            try:
                payload = await _today_payload(client, route)
            except Exception:
                log.exception("push check failed for route '%s'", route["name"])
                continue
            severity = payload.get("incident_severity", "clear")
            if not _should_push(route["name"], severity, today_iso):
                continue
            best = payload.get("best_departure_time")
            note = payload.get("incident_note", "")
            suffix = f" Best departure now: {best}." if best else ""
            await send_push(
                client,
                title=f"Commute {severity}: {route['name']}",
                message=f"{note}{suffix}",
                severity=severity,
                data={
                    "route": route["name"],
                    "best_departure_time": best,
                    "delta_minutes": payload.get("incident_delta_minutes"),
                },
            )
            log.info("Pushed %s for route '%s'", severity, route["name"])


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        daily_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=SCHEDULER_HOUR,
            minute=SCHEDULER_MINUTE,
        ),
        id="daily_recompute",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    if push_enabled():
        _scheduler.add_job(
            push_check_job,
            IntervalTrigger(minutes=max(1, PUSH_CHECK_MINUTES)),
            id="push_check",
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )
    _scheduler.start()
    log.info(
        "Scheduler started: daily recompute at %02d:%02d%s",
        SCHEDULER_HOUR,
        SCHEDULER_MINUTE,
        f"; push check every {PUSH_CHECK_MINUTES} min" if push_enabled() else "",
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
