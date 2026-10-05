"""Sentry error reporting. Off unless SENTRY_DSN is set; a no-op when
sentry_sdk is not installed.

Students' audio and transcripts are personal data, so nothing from the
request beyond method/URL/route leaves the process: no bodies, cookies,
auth headers, query strings or stack-frame local variables.
"""

try:
    import sentry_sdk
except ImportError:  # pragma: no cover
    sentry_sdk = None

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key"}


def _scrub(event, _hint):
    request = event.get("request")
    if request:
        for key in ("data", "cookies", "query_string", "env"):
            request.pop(key, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}
    return event


def configure_error_reporting(dsn: str | None, environment: str, traces_sample_rate: float) -> bool:
    if not dsn or sentry_sdk is None:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=_scrub,
        before_send_transaction=_scrub,
    )
    return True
