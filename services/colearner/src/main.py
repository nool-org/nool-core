from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.auth import configure_identity_provider
from shared.auth.providers.firebase import FirebaseIdentityProvider
from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing

from src.api.routes import cascaded, chained, health, homework_insight, multimodal, playground, ready, realtime
from src.config.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level)
    logger = get_logger(__name__)

    # Registered once per process, not per-request - see
    # services/auth/src/main.py's identical call for why.
    configure_identity_provider(
        FirebaseIdentityProvider(settings.firebase_project_id, settings.firebase_credentials_path)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("startup_complete")
        yield
        logger.info("shutdown_complete")

    app = FastAPI(title="nool-core Colearner Service", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)

    app.include_router(health.router)
    app.include_router(ready.router)
    app.include_router(chained.router)
    app.include_router(multimodal.router)
    app.include_router(homework_insight.router)
    app.include_router(realtime.router)
    app.include_router(cascaded.router)
    app.include_router(playground.router)

    return app


app = create_app()
