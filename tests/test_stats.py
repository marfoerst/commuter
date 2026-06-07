from app.services.stats import (
    classify_incident,
    percentile,
    summarize,
    window_edge_hint,
)


def test_percentile_basic():
    vals = [10, 20, 30, 40, 50]
    assert percentile(vals, 0) == 10
    assert percentile(vals, 100) == 50
    assert percentile(vals, 50) == 30
    assert percentile([], 90) is None
    assert percentile([42], 90) == 42


def test_summarize():
    s = summarize([30, 30, 32, 60])
    assert s["count"] == 4
    assert s["typical_minutes"] == 31.0  # median of 30,30,32,60
    assert s["min_minutes"] == 30.0
    assert s["max_minutes"] == 60.0
    # p90 well above median -> non-zero reliability spread
    assert s["reliability_minutes"] > 0
    assert summarize([]) is None


def test_classify_incident_uses_baseline():
    # +25 over a 30-min typical -> alert by absolute delta
    assert classify_incident(55, 30) == ("alert", 25)
    # +12 -> watch
    assert classify_incident(42, 30) == ("watch", 12)
    # small bump -> clear
    assert classify_incident(33, 30) == ("clear", 3)
    # 1.5x ratio -> alert even if absolute delta < 20
    assert classify_incident(15, 10)[0] == "alert"
    # missing data is never an incident
    assert classify_incident(None, 30) == ("clear", 0)
    assert classify_incident(50, None) == ("clear", 0)


def test_window_edge_hint_early_and_late():
    # fastest at the first slot -> suggest starting earlier
    early = [
        {"departure_time": "07:00", "duration_minutes": 30},
        {"departure_time": "07:30", "duration_minutes": 40},
        {"departure_time": "08:00", "duration_minutes": 50},
    ]
    h = window_edge_hint(early, has_deadline=False)
    assert h and h["edge"] == "early" and h["slot"] == "07:00"

    # fastest at the last slot -> suggest extending later
    late = [
        {"departure_time": "07:00", "duration_minutes": 50},
        {"departure_time": "07:30", "duration_minutes": 40},
        {"departure_time": "08:00", "duration_minutes": 30},
    ]
    h = window_edge_hint(late, has_deadline=True)
    assert h and h["edge"] == "late" and h["slot"] == "08:00"


def test_window_edge_hint_interior_is_none():
    interior = [
        {"departure_time": "07:00", "duration_minutes": 45},
        {"departure_time": "07:30", "duration_minutes": 30},
        {"departure_time": "08:00", "duration_minutes": 48},
    ]
    assert window_edge_hint(interior, has_deadline=False) is None
    # too few slots to judge
    assert window_edge_hint(interior[:2], has_deadline=False) is None
