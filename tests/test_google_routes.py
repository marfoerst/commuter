from app.services.google_routes import _duration_to_minutes, _route_label


def test_duration_to_minutes():
    assert _duration_to_minutes("600s") == 10.0
    assert _duration_to_minutes("930s") == 15.5
    assert _duration_to_minutes(None) is None
    assert _duration_to_minutes("oops") is None


def test_route_label_prefers_description():
    assert _route_label({"description": "A565 and B56"}, 0) == "A565 and B56"


def test_route_label_falls_back_to_labels():
    label = _route_label({"routeLabels": ["FUEL_EFFICIENT", "DEFAULT_ROUTE"]}, 1)
    assert label == "Fuel Efficient"


def test_route_label_generic():
    assert _route_label({}, 0) == "Default route"
    assert _route_label({"routeLabels": ["DEFAULT_ROUTE"]}, 2) == "Alternative 2"
