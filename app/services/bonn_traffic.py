"""Bonn real-time street-traffic open data.

A free, CC-BY GeoJSON feed (refreshed every 5 minutes) of ~111 road segments
covering Bonn's three Rhine bridges and major arterials. Each feature is a
``MultiLineString`` with properties:

  - ``strecke_id``     : stable integer segment id
  - ``auswertezeit``   : evaluation time (ISO-8601, UTC)
  - ``geschwindigkeit``: current speed in km/h
  - ``verkehrsstatus`` : one of the four German status strings below

The feed is *not* a router — it carries no travel times. It is used here as an
independent, Bonn-local congestion signal that complements the Google-derived
numbers and feeds the incident logic. The pure helpers (polyline decode,
geometry matching, status classification, summary) are stdlib-only so they can
be unit-tested without network — mirroring ``stats.py``.

Source: https://opendata.bonn.de/dataset/strassenverkehrslage-realtime
Attribution (CC-BY): "Datenquelle: Bundesstadt Bonn, Amt 66".
"""

from __future__ import annotations

import json
import logging
import math
import time

import httpx

from app.config import (
    BONN_CACHE_SECONDS,
    BONN_MATCH_MIN_FRACTION,
    BONN_MATCH_RADIUS_M,
    BONN_TRAFFIC_URL,
)

log = logging.getLogger(__name__)

ATTRIBUTION = "Datenquelle: Bundesstadt Bonn, Amt 66"

# The feed is served as Latin-9 (ISO-8859-15) despite advertising charset=utf-8,
# so "erhöhte" arrives as the byte 0xF6. Decode explicitly before json.loads.
FEED_ENCODING = "iso-8859-15"

# Map each verkehrsstatus to the app's incident severity vocabulary. "Staugefahr"
# (jam risk) is the feed's worst category; "aktuell nicht ermittelbar" means the
# sensor has no reading, which must never raise an alert.
STATUS_SEVERITY = {
    "Staugefahr": "alert",
    "erhöhte Verkehrsbelastung": "watch",
    "normales Verkehrsaufkommen": "clear",
    "aktuell nicht ermittelbar": "clear",
}

_SEVERITY_RANK = {"alert": 2, "watch": 1, "clear": 0}

# Module-level cache shared across requests and both directions. The feed only
# changes every 5 min, so a short TTL keeps live /today requests free of repeated
# network round-trips. Keyed by nothing (single feed URL); stores monotonic time.
_cache: dict[str, object] = {"at": None, "data": None}


def status_to_severity(status: str | None) -> str:
    """Map a verkehrsstatus string to 'alert' | 'watch' | 'clear' (default clear)."""
    return STATUS_SEVERITY.get((status or "").strip(), "clear")


async def fetch_traffic(client: httpx.AsyncClient) -> list[dict] | None:
    """Fetch and parse the Bonn traffic feed. Returns the feature list, or None.

    Best-effort: any network/parse failure returns None and callers degrade to
    "no local-traffic signal". Results are cached for ``BONN_CACHE_SECONDS``.
    """
    now = time.monotonic()
    at = _cache["at"]
    if at is not None and (now - float(at)) < BONN_CACHE_SECONDS:
        return _cache["data"]  # type: ignore[return-value]

    try:
        resp = await client.get(BONN_TRAFFIC_URL, timeout=20.0)
        resp.raise_for_status()
        text = resp.content.decode(FEED_ENCODING)
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 — best-effort enrichment, never fatal
        log.warning("Bonn traffic fetch failed: %s", e)
        return None

    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        log.warning("Bonn traffic feed had no feature list")
        return None

    _cache["at"] = now
    _cache["data"] = features
    return features


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google encoded polyline into ``[(lat, lng), ...]``."""
    if not encoded:
        return []
    factor = float(10**precision)
    points: list[tuple[float, float]] = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for _is_lng in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if _is_lng:
                lng += delta
            else:
                lat += delta
        points.append((lat / factor, lng / factor))
    return points


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lng) points."""
    lat1, lng1 = a
    lat2, lng2 = b
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def densify(
    points: list[tuple[float, float]], step_m: float = 10.0
) -> list[tuple[float, float]]:
    """Insert intermediate vertices so consecutive points are ≤ ``step_m`` apart.

    A nearest-*vertex* distance test on a densified line approximates a
    nearest-*segment* distance, which keeps segment matching accurate even when
    Google returns sparse polyline vertices on long straight stretches.
    """
    if len(points) < 2:
        return list(points)
    out: list[tuple[float, float]] = [points[0]]
    for a, b in zip(points, points[1:]):
        d = haversine_m(a, b)
        n = int(d // step_m)
        for i in range(1, n + 1):
            t = (i * step_m) / d
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return out


def _segment_points(feature: dict) -> list[tuple[float, float]]:
    """Flatten a feature's MultiLineString coordinates into [(lat, lng), ...].

    GeoJSON coordinates are [lng, lat]; we return (lat, lng) to match the rest
    of this module.
    """
    geom = feature.get("geometry") or {}
    if geom.get("type") != "MultiLineString":
        return []
    pts: list[tuple[float, float]] = []
    for line in geom.get("coordinates") or []:
        for coord in line:
            if len(coord) >= 2:
                pts.append((float(coord[1]), float(coord[0])))
    return pts


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return min(lats), min(lngs), max(lats), max(lngs)


def match_route_segments(
    route_latlng: list[tuple[float, float]],
    features: list[dict],
    radius_m: float = BONN_MATCH_RADIUS_M,
    min_fraction: float = BONN_MATCH_MIN_FRACTION,
) -> list[int]:
    """Return the ``strecke_id``s of Bonn segments that run along the route.

    A segment matches when at least ``min_fraction`` of its vertices fall within
    ``radius_m`` of the (densified) route polyline. A bounding-box prefilter
    (padded by ``radius_m``) skips the far-away majority cheaply.
    """
    if not route_latlng or not features:
        return []
    dense = densify(route_latlng)
    rmin_lat, rmin_lng, rmax_lat, rmax_lng = _bbox(dense)
    # ~metres-to-degrees padding for the bbox prefilter (latitude ~111km/deg).
    pad = radius_m / 111000.0

    matched: list[int] = []
    for feat in features:
        seg_pts = _segment_points(feat)
        if not seg_pts:
            continue
        smin_lat, smin_lng, smax_lat, smax_lng = _bbox(seg_pts)
        if (
            smax_lat < rmin_lat - pad
            or smin_lat > rmax_lat + pad
            or smax_lng < rmin_lng - pad
            or smin_lng > rmax_lng + pad
        ):
            continue
        near = 0
        for sp in seg_pts:
            if any(haversine_m(sp, rp) <= radius_m for rp in dense):
                near += 1
        if near / len(seg_pts) >= min_fraction:
            sid = (feat.get("properties") or {}).get("strecke_id")
            if sid is not None:
                matched.append(int(sid))
    return sorted(set(matched))


def summarize_local_traffic(
    features: list[dict] | None, segment_ids: list[int]
) -> dict | None:
    """Summarize the live status of a route's matched segments, or None.

    Returns ``None`` when there is no feed or no matched segment present in it,
    so callers simply omit the panel. Otherwise:

        {
          "source": "Bonn open-data",
          "evaluated_at": "2026-06-25T10:50:00Z",
          "severity": "alert",                 # worst across matched segments
          "worst_status": "Staugefahr",
          "min_speed_kmh": 5,
          "segment_count": 6,
          "congested": [ {strecke_id, status, speed_kmh}, ... ],  # watch/alert only
          "attribution": "Datenquelle: Bundesstadt Bonn, Amt 66",
        }
    """
    if not features or not segment_ids:
        return None
    want = set(segment_ids)
    rows: list[dict] = []
    for feat in features:
        props = feat.get("properties") or {}
        sid = props.get("strecke_id")
        if sid is None or int(sid) not in want:
            continue
        status = (props.get("verkehrsstatus") or "").strip()
        rows.append(
            {
                "strecke_id": int(sid),
                "status": status,
                "speed_kmh": props.get("geschwindigkeit"),
                "severity": status_to_severity(status),
                "evaluated_at": props.get("auswertezeit"),
            }
        )
    if not rows:
        return None

    worst = max(rows, key=lambda r: _SEVERITY_RANK[r["severity"]])
    # Slowest *measured* segment (ignore "nicht ermittelbar" which reports 0/None).
    speeds = [
        r["speed_kmh"]
        for r in rows
        if r["status"] != "aktuell nicht ermittelbar" and r["speed_kmh"] is not None
    ]
    congested = sorted(
        (
            {"strecke_id": r["strecke_id"], "status": r["status"], "speed_kmh": r["speed_kmh"]}
            for r in rows
            if r["severity"] != "clear"
        ),
        key=lambda r: (r["speed_kmh"] is None, r["speed_kmh"]),
    )
    return {
        "source": "Bonn open-data",
        "evaluated_at": worst.get("evaluated_at") or (rows[0].get("evaluated_at")),
        "severity": worst["severity"],
        "worst_status": worst["status"],
        "min_speed_kmh": min(speeds) if speeds else None,
        "segment_count": len(rows),
        "congested": congested,
        "attribution": ATTRIBUTION,
    }
