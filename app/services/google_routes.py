import logging
from datetime import datetime, timezone

import httpx

from app.config import GOOGLE_API_KEY

log = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


async def compute_route_duration(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    departure_time: datetime,
) -> float | None:
    """Query Google Routes API. Returns duration in minutes, or None on failure."""
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    if departure_time.tzinfo is None:
        departure_time = departure_time.astimezone()
    departure_iso = departure_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters",
    }
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": departure_iso,
    }

    try:
        resp = await client.post(ROUTES_URL, json=body, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.warning(
            "Routes API HTTP %s for %s -> %s @ %s: %s",
            e.response.status_code,
            origin,
            destination,
            departure_iso,
            e.response.text[:300],
        )
        return None
    except Exception as e:
        log.warning("Routes API call failed: %s", e)
        return None

    data = resp.json()
    routes = data.get("routes") or []
    if not routes:
        return None
    duration_str = routes[0].get("duration") or "0s"
    try:
        seconds = int(duration_str.rstrip("s"))
    except ValueError:
        return None
    return round(seconds / 60.0, 1)
