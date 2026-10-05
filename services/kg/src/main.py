from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing

from src.api.routes import curriculum, health, question_paper, ready, voice_test
from src.config.settings import get_settings
from src.services.nool_kg_client import dispose_client, init_client


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level)
    logger = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_client(settings.nool_kg_base_url, settings.nool_kg_api_key)
        logger.info("startup_complete")
        yield
        await dispose_client()
        logger.info("shutdown_complete")

    app = FastAPI(title="nool-core KG Service", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)

    app.include_router(health.router)
    app.include_router(ready.router)
    app.include_router(curriculum.router)
    app.include_router(question_paper.router)
    app.include_router(voice_test.router)

    return app


app = create_app()
