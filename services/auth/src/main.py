from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.auth import configure_identity_provider
from shared.auth.providers.firebase import FirebaseIdentityProvider
from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing
from shared.observability import configure_observability

from src.api.routes import auth, health, ready
from src.config.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level)
    logger = get_logger(__name__)

    # Registered once per process, not per-request: firebase_admin's app
    # initialization is process-global, so a fresh FirebaseIdentityProvider
    # built on every request would collide with it on the second request
    # onward ("the default Firebase app already exists").
    configure_identity_provider(
        FirebaseIdentityProvider(
            settings.firebase_project_id,
            settings.firebase_credentials_path,
            settings.firebase_web_api_key,
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("startup_complete")
        yield
        logger.info("shutdown_complete")

    app = FastAPI(title="nool-core Auth Service", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)
    configure_observability(app, settings)

    app.include_router(health.router)
    app.include_router(ready.router)
    app.include_router(auth.router)

    return app


app = create_app()
