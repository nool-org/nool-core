from fastapi import FastAPI

from shared.config import BaseServiceSettings
from shared.error_reporting import configure_error_reporting
from shared.metrics import configure_metrics


def configure_observability(app: FastAPI, settings: BaseServiceSettings) -> None:
    """Prometheus /metrics + Sentry (when SENTRY_DSN is set). One call per service."""
    configure_metrics(app, settings.service_name)
    configure_error_reporting(settings.sentry_dsn, settings.environment, settings.sentry_traces_sample_rate)
