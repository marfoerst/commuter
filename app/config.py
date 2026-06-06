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
