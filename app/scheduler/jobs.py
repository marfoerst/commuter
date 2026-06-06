import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import SCHEDULER_HOUR, SCHEDULER_MINUTE
from app.services.sampling import recompute_all_active_routes

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def daily_job() -> None:
    try:
        # Refresh only today's forecast column; the rest of the week's snapshot
        # stays put. Cuts the daily batch's Google Routes API calls ~5x vs.
        # re-sampling the whole week every morning.
        counts = await recompute_all_active_routes(only_today=True)
        log.info("Daily recompute complete (today only): %s", counts)
    except Exception:
        log.exception("Daily recompute failed")


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
    _scheduler.start()
    log.info(
        "Scheduler started: daily recompute at %02d:%02d",
        SCHEDULER_HOUR, SCHEDULER_MINUTE,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
