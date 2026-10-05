"""Prometheus metrics shared by every service.

configure_metrics(app, service_name) adds an ASGI middleware recording
  nool_http_requests_total{service,method,route,status}
  nool_http_request_duration_seconds{service,method,route,status}  (histogram)
  nool_http_requests_in_flight{service}
and mounts GET /metrics. `route` is the route template (/items/{id}), never
the raw path, so label cardinality stays bounded; unmatched paths are
"unmatched". /metrics is not routed by nginx - scrape it on the internal
network only.

LLM spend helpers other code may call (no-ops when prometheus_client is
missing):
  record_llm_cost("colearner", "cascaded", 0.0012)
  record_llm_tokens("colearner", "cascaded", "gpt-4o-mini", input_tokens=812, output_tokens=96)

prometheus_client is optional: without it everything here is a no-op, so
images built before it was added to requirements.txt still boot.
"""

import time

from fastapi import FastAPI

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except ImportError:  # pragma: no cover - exercised when the lib is absent
    Counter = None

if Counter is not None:
    REQUESTS = Counter("nool_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
    LATENCY = Histogram(
        "nool_http_request_duration_seconds", "HTTP request latency", ["service", "method", "route", "status"]
    )
    IN_FLIGHT = Gauge("nool_http_requests_in_flight", "HTTP requests in progress", ["service"])
    LLM_COST = Counter("nool_llm_cost_usd_total", "LLM spend in USD", ["service", "pipeline"])
    LLM_TOKENS = Counter("nool_llm_tokens_total", "LLM tokens", ["service", "pipeline", "model", "kind"])


def record_llm_cost(service: str, pipeline: str, usd: float) -> None:
    if Counter is not None and usd > 0:
        LLM_COST.labels(service, pipeline).inc(usd)


def record_llm_tokens(service: str, pipeline: str, model: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
    if Counter is None:
        return
    if input_tokens > 0:
        LLM_TOKENS.labels(service, pipeline, model, "input").inc(input_tokens)
    if output_tokens > 0:
        LLM_TOKENS.labels(service, pipeline, model, "output").inc(output_tokens)


class MetricsMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so streaming responses are timed to
    completion and websocket traffic passes through untouched."""

    def __init__(self, app, service_name: str) -> None:
        self.app = app
        self.service = service_name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = 500
        start = time.perf_counter()

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        IN_FLIGHT.labels(self.service).inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            IN_FLIGHT.labels(self.service).dec()
            route = getattr(scope.get("route"), "path", "unmatched")
            labels = (self.service, scope["method"], route, str(status))
            REQUESTS.labels(*labels).inc()
            LATENCY.labels(*labels).observe(time.perf_counter() - start)


def configure_metrics(app: FastAPI, service_name: str) -> None:
    if Counter is None:
        return
    app.add_middleware(MetricsMiddleware, service_name=service_name)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        from fastapi import Response

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
