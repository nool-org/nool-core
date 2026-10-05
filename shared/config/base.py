from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common environment-driven configuration every service inherits.

    Subclasses set their own ``service_name`` default and add
    service-specific fields (database credentials, Firebase config, ...).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str
    environment: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    # OTLP HTTP traces endpoint (e.g. http://jaeger:4318/v1/traces) - see
    # shared/tracing/setup.py. Unset by default: tracing is optional and a
    # service must boot cleanly without a collector available.
    otel_exporter_otlp_endpoint: str | None = None
    # shared/error_reporting: Sentry is off unless SENTRY_DSN is set.
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.05
