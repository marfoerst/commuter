import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_PATH = DATA_DIR / "commute.db"

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()

DEFAULT_ORIGIN = os.environ.get("DEFAULT_ORIGIN", "").strip()
DEFAULT_DESTINATION = os.environ.get("DEFAULT_DESTINATION", "").strip()
TIME_WINDOW_START = os.environ.get("TIME_WINDOW_START", "07:00").strip()
TIME_WINDOW_END = os.environ.get("TIME_WINDOW_END", "09:00").strip()
EVENING_TIME_WINDOW_START = os.environ.get("EVENING_TIME_WINDOW_START", "16:00").strip()
EVENING_TIME_WINDOW_END = os.environ.get("EVENING_TIME_WINDOW_END", "18:30").strip()
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "15"))
DEFAULT_WEEKDAYS = os.environ.get("DEFAULT_WEEKDAYS", "Mon,Tue,Wed,Thu,Fri").strip()

SCHEDULER_HOUR = int(os.environ.get("SCHEDULER_HOUR", "4"))
SCHEDULER_MINUTE = int(os.environ.get("SCHEDULER_MINUTE", "0"))

CONCURRENT_REQUESTS = int(os.environ.get("CONCURRENT_REQUESTS", "10"))

API_KEY = os.environ.get("API_KEY", "").strip()

# Proactive push (opt-in). Set either to enable a periodic in-window check that
# notifies you when live conditions cross PUSH_MIN_SEVERITY. Costs extra Routes
# API calls only while a commute window is open — see docs/OPERATIONS.md.
NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()  # e.g. https://ntfy.sh/my-commute
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()  # generic JSON POST target
PUSH_MIN_SEVERITY = os.environ.get("PUSH_MIN_SEVERITY", "alert").strip().lower()  # watch|alert
PUSH_CHECK_MINUTES = int(os.environ.get("PUSH_CHECK_MINUTES", "15"))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Bonn real-time street-traffic open data (free, CC-BY, 5-min refresh). A live,
# Bonn-local congestion signal that complements the Google-derived numbers and
# feeds the incident logic. Segments are auto-matched to each route's geometry
# once (at config/recompute time); the live path only does one cached GET.
# See https://opendata.bonn.de/dataset/strassenverkehrslage-realtime
BONN_TRAFFIC_ENABLED = _env_bool("BONN_TRAFFIC_ENABLED", True)
BONN_TRAFFIC_URL = os.environ.get(
    "BONN_TRAFFIC_URL", "https://stadtplan.bonn.de/geojson?Thema=19584"
).strip()
BONN_CACHE_SECONDS = int(os.environ.get("BONN_CACHE_SECONDS", "300"))
# Segment-to-route matching tolerances (used only when (re)matching a route).
BONN_MATCH_RADIUS_M = float(os.environ.get("BONN_MATCH_RADIUS_M", "40"))
BONN_MATCH_MIN_FRACTION = float(os.environ.get("BONN_MATCH_MIN_FRACTION", "0.5"))
