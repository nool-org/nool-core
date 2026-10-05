"""shared/metrics + shared/error_reporting: real metrics when
prometheus_client is installed, a clean no-op when it is not."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared import metrics
from shared.error_reporting import _scrub, configure_error_reporting


def _app():
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    metrics.configure_metrics(app, "test-svc")
    return TestClient(app)


def test_metrics_endpoint_or_noop():
    client = _app()
    assert client.get("/items/7").status_code == 200
    metrics.record_llm_cost("test-svc", "unit", 0.25)
    metrics.record_llm_tokens("test-svc", "unit", "m", input_tokens=10, output_tokens=3)
    resp = client.get("/metrics")
    if metrics.Counter is None:
        assert resp.status_code == 404
        return
    assert resp.status_code == 200
    body = resp.text
    assert 'nool_http_requests_total{method="GET",route="/items/{item_id}",service="test-svc",status="200"}' in body
    assert "nool_http_request_duration_seconds_bucket" in body
    assert "nool_http_requests_in_flight" in body
    assert 'nool_llm_cost_usd_total{pipeline="unit",service="test-svc"} 0.25' in body
    assert 'kind="output",model="m",pipeline="unit",service="test-svc"} 3.0' in body


def test_error_reporting_off_without_dsn():
    assert configure_error_reporting(None, "test", 0.05) is False


def test_scrub_drops_bodies_and_credentials():
    event = {"request": {"data": "student transcript", "cookies": {"a": "b"}, "query_string": "t=x",
                         "headers": {"Authorization": "Bearer x", "User-Agent": "ua"}, "url": "/x"}}
    req = _scrub(event, None)["request"]
    assert req == {"headers": {"User-Agent": "ua"}, "url": "/x"}
