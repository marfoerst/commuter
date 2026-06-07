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


def _duration_to_minutes(duration_str: str | None) -> float | None:
    if not duration_str:
        return None
    try:
        return round(int(duration_str.rstrip("s")) / 60.0, 1)
    except ValueError:
        return None


def _route_label(route: dict, index: int) -> str:
    """Human label for an alternative: prefer the road summary, fall back to
    route labels, then a generic name."""
    desc = (route.get("description") or "").strip()
    if desc:
        return desc
    labels = route.get("routeLabels") or []
    pretty = [
        lbl.replace("_", " ").title()
        for lbl in labels
        if lbl != "DEFAULT_ROUTE"
    ]
    if pretty:
        return ", ".join(pretty)
    return "Default route" if index == 0 else f"Alternative {index}"


async def compute_route_alternatives(
    client: httpx.AsyncClient,
    origin: str,
    destination: str,
    departure_time: datetime,
    max_alternatives: int = 3,
) -> list[dict]:
    """Return up to ``max_alternatives`` distinct routes for one departure time,
    sorted fastest-first: ``[{label, duration_minutes, distance_km}]``.

    This answers "which crossing/detour is fastest right now" — the key question
    when a bridge closure forces traffic onto competing corridors. All
    alternatives come back in a single billable request.

    Note: ``computeAlternativeRoutes`` is not supported with
    TRAFFIC_AWARE_OPTIMAL, so this uses TRAFFIC_AWARE (still real-time traffic,
    one tier down). Returns ``[]`` on any failure — callers treat alternatives
    as best-effort enrichment, never a hard dependency.
    """
    if not GOOGLE_API_KEY:
        return []
    if departure_time.tzinfo is None:
        departure_time = departure_time.astimezone()
    departure_iso = departure_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": (
            "routes.duration,routes.distanceMeters,"
            "routes.description,routes.routeLabels"
        ),
    }
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": True,
        "departureTime": departure_iso,
    }

    try:
        resp = await client.post(ROUTES_URL, json=body, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.warning(
            "Routes API (alternatives) HTTP %s for %s -> %s: %s",
            e.response.status_code, origin, destination, e.response.text[:300],
        )
        return []
    except Exception as e:
        log.warning("Routes API (alternatives) call failed: %s", e)
        return []

    out: list[dict] = []
    for i, r in enumerate(resp.json().get("routes") or []):
        minutes = _duration_to_minutes(r.get("duration"))
        if minutes is None:
            continue
        dist = r.get("distanceMeters")
        out.append(
            {
                "label": _route_label(r, i),
                "duration_minutes": round(minutes),
                "distance_km": round(dist / 1000.0, 1) if dist else None,
            }
        )
    out.sort(key=lambda x: x["duration_minutes"])
    return out[:max_alternatives]
