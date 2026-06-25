import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth_routes import router as auth_router
from app.api.routes import router as api_router
from app.config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    DEFAULT_DESTINATION,
    DEFAULT_ORIGIN,
    DEFAULT_WEEKDAYS,
    EVENING_TIME_WINDOW_END,
    EVENING_TIME_WINDOW_START,
    INTERVAL_MINUTES,
    NTFY_TOPIC_URL,
    PUSH_MIN_SEVERITY,
    TIME_WINDOW_END,
    TIME_WINDOW_START,
    WEBHOOK_URL,
)
from app.db.database import get_conn, init_db
from app.db.models import get_route_by_name, upsert_named_route
from app.db.users import (
    count_users,
    create_user,
    get_user_by_id,
    get_user_by_username,
    purge_expired_sessions,
    update_user_push_settings,
)
from app.scheduler.jobs import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def seed_admin_and_migrate() -> None:
    """On first start, create the admin account and adopt any pre-multi-user
    routes. Idempotent: safe to run on every boot.

    1. If no users exist, create the admin from ADMIN_USERNAME/ADMIN_PASSWORD
       (a default password is used with a loud warning if none is set), and seed
       the admin's push prefs from the legacy global NTFY/WEBHOOK env vars.
    2. Backfill any routes left over from the single-tenant schema (user_id NULL)
       onto the admin, so existing data keeps working.
    3. Seed the admin's morning/evening from DEFAULT_ORIGIN/DESTINATION if they
       have none yet.
    """
    admin = get_user_by_username(ADMIN_USERNAME)
    if count_users() == 0:
        password = ADMIN_PASSWORD or "changeme"
        admin_id = create_user(ADMIN_USERNAME, password, is_admin=True)
        admin = get_user_by_id(admin_id)
        if not ADMIN_PASSWORD:
            log.warning(
                "No ADMIN_PASSWORD set — created admin '%s' with default password "
                "'changeme'. CHANGE IT IMMEDIATELY via the Settings tab.",
                ADMIN_USERNAME,
            )
        else:
            log.info("Created admin account '%s'", ADMIN_USERNAME)
        if NTFY_TOPIC_URL or WEBHOOK_URL:
            update_user_push_settings(
                admin_id, NTFY_TOPIC_URL or None, WEBHOOK_URL or None, PUSH_MIN_SEVERITY
            )

    if admin is None:
        return

    # Adopt orphaned (pre-migration) routes onto the admin.
    with get_conn() as conn:
        n = conn.execute(
            "UPDATE routes SET user_id = ? WHERE user_id IS NULL", (admin["id"],)
        ).rowcount
    if n:
        log.info("Migrated %d existing route(s) to admin '%s'", n, ADMIN_USERNAME)

    # Seed default routes for the admin if they have none.
    if DEFAULT_ORIGIN and DEFAULT_DESTINATION:
        if not get_route_by_name(admin["id"], "morning"):
            upsert_named_route(
                admin["id"], "morning", DEFAULT_ORIGIN, DEFAULT_DESTINATION,
                TIME_WINDOW_START, TIME_WINDOW_END, INTERVAL_MINUTES, DEFAULT_WEEKDAYS,
            )
        if not get_route_by_name(admin["id"], "evening"):
            upsert_named_route(
                admin["id"], "evening", DEFAULT_DESTINATION, DEFAULT_ORIGIN,
                EVENING_TIME_WINDOW_START, EVENING_TIME_WINDOW_END,
                INTERVAL_MINUTES, DEFAULT_WEEKDAYS,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_admin_and_migrate()
    purge_expired_sessions()
    start_scheduler()
    log.info("Commute Optimizer started")
    try:
        yield
    finally:
        stop_scheduler()
        log.info("Commute Optimizer stopped")


app = FastAPI(title="Commute Optimizer", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(api_router)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
