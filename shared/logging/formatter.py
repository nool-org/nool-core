import json
import logging
from datetime import datetime, timezone

from opentelemetry import trace

from .context import get_request_id

_EXTRA_FIELDS = ("route", "method", "status", "duration_ms", "reason", "error", "event", "uid", "cost")


class JSONFormatter(logging.Formatter):
    """Renders log records as single-line JSON with the fields required by CLAUDE.md:

    timestamp, level, service, request_id, route, status, duration.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "message": record.getMessage(),
        }

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id

        # Correlates this log line with its distributed trace (see
        # shared/tracing/) - a no-op, always-valid call even when tracing
        # isn't configured (get_current_span() then returns an invalid
        # span, which the is_valid check below simply skips).
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)
