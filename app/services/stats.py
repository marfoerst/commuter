"""Pure statistical helpers over accumulated observations.

These functions are deliberately dependency-free (stdlib only) so they can be
unit-tested without a database or network. They power three features that all
rely on having *history* for a given (route, weekday, slot):

  - reliability  : how spread out are the observed durations (median vs p90)
  - incident     : is today's live drive worse than the typical drive for this
                   slot, rather than worse than this single morning's forecast
  - window edge  : is the best slot sitting at the edge of the sampling window,
                   suggesting the window should be widened

When there is little or no history every helper degrades gracefully (returns
None / "clear"), and callers fall back to the previous snapshot-only behaviour.
"""

from __future__ import annotations

import math
import statistics

# Below this many observations a slot's history is too thin to trust for
# typical/p90 or for incident comparison.
MIN_SAMPLES_FOR_STATS = 4

INCIDENT_ALERT_DELTA_MIN = 20
INCIDENT_WATCH_DELTA_MIN = 10
INCIDENT_ALERT_RATIO = 1.5


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolation percentile. ``p`` in [0, 100]. None if empty."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def summarize(durations: list[float]) -> dict | None:
    """Summary stats for a slot's observed durations, or None if empty.

    ``reliability`` is the median→p90 spread in minutes — a practical "how much
    worse than typical can this slot get" number used to size a safety buffer.
    """
    vals = [float(d) for d in durations if d is not None]
    if not vals:
        return None
    median = statistics.median(vals)
    p90 = percentile(vals, 90) or median
    return {
        "count": len(vals),
        "typical_minutes": round(median, 1),
        "p90_minutes": round(p90, 1),
        "min_minutes": round(min(vals), 1),
        "max_minutes": round(max(vals), 1),
        "reliability_minutes": round(max(0.0, p90 - median), 1),
    }


def classify_incident(
    live_dur: float | None, baseline_dur: float | None
) -> tuple[str, int]:
    """Compare a live duration to a baseline. Returns (severity, delta_minutes).

    ``baseline_dur`` should be the *typical* duration for this slot (trailing
    median) when enough history exists, otherwise this morning's snapshot. The
    point of using the trailing median is that on a chronically congested
    corridor the snapshot itself already bakes in the jam, so "live vs snapshot"
    stops firing; "live vs typical-for-this-slot" still flags the genuinely bad
    days.
    """
    if live_dur is None or baseline_dur is None or baseline_dur <= 0:
        return "clear", 0
    delta = int(round(float(live_dur) - float(baseline_dur)))
    ratio = float(live_dur) / float(baseline_dur)
    if delta >= INCIDENT_ALERT_DELTA_MIN or ratio >= INCIDENT_ALERT_RATIO:
        return "alert", delta
    if delta >= INCIDENT_WATCH_DELTA_MIN:
        return "watch", delta
    return "clear", delta


def window_edge_hint(
    day_data: list[dict],
    has_deadline: bool,
) -> dict | None:
    """Detect when the best slot sits at the edge of the sampling window.

    ``day_data`` is the snapshot for today: ``[{departure_time, duration_minutes}]``.

    A bridge closure pushes the rush-hour peak earlier and later than it used to
    be. If the shortest-drive slot is the very first sampled slot, the true
    optimum is probably *before* the window starts (leave even earlier); if it's
    the last slot, extend the window later. Returns None when the best slot is
    safely in the interior, or when there are too few slots to judge.
    """
    if len(day_data) < 3:
        return None
    ordered = sorted(day_data, key=lambda d: d["departure_time"])
    best = min(ordered, key=lambda d: float(d["duration_minutes"]))
    first, last = ordered[0], ordered[-1]
    if best["departure_time"] == first["departure_time"]:
        return {
            "edge": "early",
            "slot": first["departure_time"],
            "message": (
                f"Fastest drive is at the very start of your window "
                f"({first['departure_time']}). Traffic may be lighter even "
                f"earlier — consider starting the window before {first['departure_time']}."
            ),
        }
    if best["departure_time"] == last["departure_time"]:
        return {
            "edge": "late",
            "slot": last["departure_time"],
            "message": (
                f"Fastest drive is at the very end of your window "
                f"({last['departure_time']}). The peak may now run later — "
                f"consider extending the window past {last['departure_time']}."
            ),
        }
    return None
