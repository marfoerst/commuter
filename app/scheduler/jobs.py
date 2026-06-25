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
    send_push,
    user_push_enabled,
)
from app.services.sampling import recompute_all_users

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# (user_id, route_name) -> (date_iso, highest severity rank already pushed today).
# Avoids re-notifying for the same (or a lower) severity within a single day.
_last_pushed: dict[tuple[int, str], tuple[str, int]] = {}


async def daily_job() -> None:
    try:
        # Refresh only today's forecast column for every user; the rest of the
        # week's snapshot stays put. Cuts the daily batch's Google Routes API
        # calls ~5x vs. re-sampling the whole week every morning.
        counts = await recompute_all_users(only_today=True)
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


def _should_push(
    user_id: int, route_name: str, severity: str, min_severity: str, today: str
) -> bool:
    """Push only the first time today the severity crosses the user's threshold,
    and again only if it escalates (watch -> alert). Resets each day."""
    if not meets_threshold(severity, min_severity):
        return False
    rank = SEVERITY_RANK.get(severity, 0)
    key = (user_id, route_name)
    last_day, last_rank = _last_pushed.get(key, ("", -1))
    if last_day != today:
        last_rank = -1
    if rank <= last_rank:
        return False
    _last_pushed[key] = (today, rank)
    return True


async def push_check_job() -> None:
    """Periodic in-window check: notify each user when their live conditions are
    bad, via that user's own push sinks.

    Skips users without push configured, and routes outside their commute
    window / on non-commute days, so it spends Google Routes API calls only when
    it could actually have something to say.
    """
    # Imported lazily to keep the scheduler module free of the API layer at
    # import time.
    from app.api.routes import _is_active_day, _today_payload
    from app.db.models import get_all_active_routes
    from app.db.users import list_users
    from app.services.sampling import WEEKDAYS

    now = datetime.now().astimezone()
    today_iso = date.today().isoformat()
    weekday = WEEKDAYS[now.weekday()]

    async with httpx.AsyncClient() as client:
        for user in list_users():
            if not user_push_enabled(user):
                continue
            routes = [
                r
                for r in get_all_active_routes(user["id"])
                if _is_active_day(r, weekday) and _within_window(r, now)
            ]
            if not routes:
                continue
            min_sev = user.get("push_min_severity", "alert")
            for route in routes:
                try:
                    payload = await _today_payload(client, route)
                except Exception:
                    log.exception(
                        "push check failed for user %s route '%s'",
                        user["id"], route["name"],
                    )
                    continue
                severity = payload.get("incident_severity", "clear")
                if not _should_push(user["id"], route["name"], severity, min_sev, today_iso):
                    continue
                best = payload.get("best_departure_time")
                note = payload.get("incident_note", "")
                suffix = f" Best departure now: {best}." if best else ""
                await send_push(
                    client,
                    user,
                    title=f"Commute {severity}: {route['name']}",
                    message=f"{note}{suffix}",
                    severity=severity,
                    data={
                        "route": route["name"],
                        "best_departure_time": best,
                        "delta_minutes": payload.get("incident_delta_minutes"),
                    },
                )
                log.info(
                    "Pushed %s to user %s for route '%s'",
                    severity, user["id"], route["name"],
                )


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
    # Push is per-user and configured at runtime, so the check is always
    # scheduled; it returns immediately for users without a push sink and only
    # spends API calls for users in an open window.
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
        "Scheduler started: daily recompute at %02d:%02d; push check every %d min",
        SCHEDULER_HOUR,
        SCHEDULER_MINUTE,
        PUSH_CHECK_MINUTES,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
