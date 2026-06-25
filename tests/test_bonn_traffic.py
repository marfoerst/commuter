"""Offline unit tests for the Bonn traffic service (no network)."""

import asyncio

import pytest

from app.services import bonn_traffic as bt


# ---------------------------------------------------------------------------
# Polyline decode + geometry helpers
# ---------------------------------------------------------------------------


def test_decode_polyline_known_vector():
    # Google's canonical example polyline.
    pts = bt.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    expected = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert len(pts) == len(expected)
    for (lat, lng), (elat, elng) in zip(pts, expected):
        assert lat == pytest.approx(elat, abs=1e-5)
        assert lng == pytest.approx(elng, abs=1e-5)


def test_decode_polyline_empty():
    assert bt.decode_polyline("") == []


def test_haversine_known_distance():
    # One degree of latitude is ~111 km.
    d = bt.haversine_m((50.0, 7.0), (51.0, 7.0))
    assert d == pytest.approx(111195, rel=0.01)


def test_densify_inserts_points_within_step():
    pts = [(50.70, 7.10), (50.70, 7.12)]  # ~1.4 km apart
    dense = bt.densify(pts, step_m=50.0)
    assert len(dense) > 10
    for a, b in zip(dense, dense[1:]):
        assert bt.haversine_m(a, b) <= 60.0  # ≤ step plus a little slack


# ---------------------------------------------------------------------------
# Segment matching
# ---------------------------------------------------------------------------


def _feature(strecke_id, coords, status="normales Verkehrsaufkommen", speed=50):
    return {
        "type": "Feature",
        "geometry": {"type": "MultiLineString", "coordinates": [coords]},
        "properties": {
            "strecke_id": strecke_id,
            "auswertezeit": "2026-06-25T10:50:00Z",
            "geschwindigkeit": speed,
            "verkehrsstatus": status,
        },
    }


def test_match_route_segments_on_and_off_route():
    # Route runs east along latitude 50.7000.
    route = [(50.7000, 7.1000), (50.7000, 7.1100)]
    # On-route: a segment lying on the same line (GeoJSON order is [lng, lat]).
    on = _feature(1, [[7.1020, 50.7000], [7.1040, 50.7000], [7.1060, 50.7000]])
    # Off-route: ~11 km north.
    off = _feature(2, [[7.1020, 50.8000], [7.1040, 50.8000]])
    matched = bt.match_route_segments(route, [on, off], radius_m=40, min_fraction=0.5)
    assert matched == [1]


def test_match_route_segments_empty_inputs():
    assert bt.match_route_segments([], [_feature(1, [[7.1, 50.7]])]) == []
    assert bt.match_route_segments([(50.7, 7.1)], []) == []


# ---------------------------------------------------------------------------
# Status classification (incl. Latin-9 umlaut) + summary
# ---------------------------------------------------------------------------


def test_status_severity_mapping():
    assert bt.status_to_severity("Staugefahr") == "alert"
    assert bt.status_to_severity("erhöhte Verkehrsbelastung") == "watch"
    assert bt.status_to_severity("normales Verkehrsaufkommen") == "clear"
    assert bt.status_to_severity("aktuell nicht ermittelbar") == "clear"
    assert bt.status_to_severity(None) == "clear"


def test_umlaut_decodes_from_latin9_and_classifies():
    # The feed ships "erhöhte" with the Latin-9 byte 0xF6 for ö.
    decoded = b"erh\xf6hte Verkehrsbelastung".decode(bt.FEED_ENCODING)
    assert decoded == "erhöhte Verkehrsbelastung"
    assert bt.status_to_severity(decoded) == "watch"


def test_summarize_local_traffic_worst_wins():
    feats = [
        _feature(1, [[7.10, 50.70]], "normales Verkehrsaufkommen", 55),
        _feature(2, [[7.11, 50.70]], "erhöhte Verkehrsbelastung", 18),
        _feature(3, [[7.12, 50.70]], "Staugefahr", 5),
        _feature(4, [[7.13, 50.70]], "aktuell nicht ermittelbar", 0),
        _feature(9, [[7.99, 50.99]], "Staugefahr", 1),  # not in segment set
    ]
    lt = bt.summarize_local_traffic(feats, [1, 2, 3, 4])
    assert lt["severity"] == "alert"
    assert lt["worst_status"] == "Staugefahr"
    assert lt["segment_count"] == 4
    # min speed ignores the "nicht ermittelbar" 0-reading.
    assert lt["min_speed_kmh"] == 5
    # congested = watch+alert only, slowest first; unknown segment excluded.
    assert [c["strecke_id"] for c in lt["congested"]] == [3, 2]
    assert lt["attribution"] == bt.ATTRIBUTION


def test_summarize_local_traffic_none_when_no_match():
    feats = [_feature(1, [[7.10, 50.70]])]
    assert bt.summarize_local_traffic(feats, [99]) is None
    assert bt.summarize_local_traffic(None, [1]) is None
    assert bt.summarize_local_traffic(feats, []) is None


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    async def get(self, url, timeout=None):
        self.calls += 1
        return _FakeResp(self._content)


def test_fetch_traffic_decodes_and_caches(monkeypatch):
    # Reset the module cache so this test is deterministic.
    bt._cache["at"] = None
    bt._cache["data"] = None
    body = (
        b'{"type":"FeatureCollection","features":'
        b'[{"properties":{"strecke_id":1,"verkehrsstatus":"erh\xf6hte Verkehrsbelastung"}}]}'
    )
    client = _FakeClient(body)

    feats = asyncio.run(bt.fetch_traffic(client))
    assert feats[0]["properties"]["verkehrsstatus"] == "erhöhte Verkehrsbelastung"
    assert client.calls == 1

    # Second call within TTL is served from cache (no extra network call).
    feats2 = asyncio.run(bt.fetch_traffic(client))
    assert feats2 is feats
    assert client.calls == 1


def test_fetch_traffic_returns_none_on_error(monkeypatch):
    bt._cache["at"] = None
    bt._cache["data"] = None

    class _Boom:
        async def get(self, url, timeout=None):
            raise RuntimeError("network down")

    assert asyncio.run(bt.fetch_traffic(_Boom())) is None
