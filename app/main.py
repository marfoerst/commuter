import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.config import (
    DEFAULT_DESTINATION,
    DEFAULT_ORIGIN,
    DEFAULT_WEEKDAYS,
    EVENING_TIME_WINDOW_END,
    EVENING_TIME_WINDOW_START,
    INTERVAL_MINUTES,
    TIME_WINDOW_END,
    TIME_WINDOW_START,
)
from app.db.database import init_db
from app.db.models import get_route_by_name, upsert_named_route
from app.scheduler.jobs import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def seed_default_routes() -> None:
    """Seed morning + evening routes from env vars on first start."""
    if not (DEFAULT_ORIGIN and DEFAULT_DESTINATION):
        return
    if not get_route_by_name("morning"):
        upsert_named_route(
            "morning",
            DEFAULT_ORIGIN,
            DEFAULT_DESTINATION,
            TIME_WINDOW_START,
            TIME_WINDOW_END,
            INTERVAL_MINUTES,
            DEFAULT_WEEKDAYS,
        )
        log.info(
            "Seeded morning route: %s -> %s", DEFAULT_ORIGIN, DEFAULT_DESTINATION
        )
    if not get_route_by_name("evening"):
        upsert_named_route(
            "evening",
            DEFAULT_DESTINATION,
            DEFAULT_ORIGIN,
            EVENING_TIME_WINDOW_START,
            EVENING_TIME_WINDOW_END,
            INTERVAL_MINUTES,
            DEFAULT_WEEKDAYS,
        )
        log.info(
            "Seeded evening route: %s -> %s", DEFAULT_DESTINATION, DEFAULT_ORIGIN
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_default_routes()
    start_scheduler()
    log.info("Commute Optimizer started")
    try:
        yield
    finally:
        stop_scheduler()
        log.info("Commute Optimizer stopped")


app = FastAPI(title="Commute Optimizer", lifespan=lifespan)
app.include_router(api_router)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
