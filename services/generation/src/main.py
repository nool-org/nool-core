from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing
from shared.observability import configure_observability

from src.api.routes import gap_fill, health, paper_answer_key, paper_copilot, voice_prep
from src.config.settings import get_settings
from src.services.llm import OpenAIStructured


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level)
    logger = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.llm = (OpenAIStructured(settings.openai_api_key, timeout=settings.request_timeout_seconds / 4)
                         if settings.openai_api_key else None)
        logger.info("startup_complete", extra={"llm_configured": app.state.llm is not None})
        yield
        logger.info("shutdown_complete")

    app = FastAPI(title="nool-core Generation Service", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)
    configure_observability(app, settings)

    app.include_router(health.router)
    app.include_router(gap_fill.router)
    app.include_router(voice_prep.router)
    app.include_router(paper_copilot.router)
    app.include_router(paper_answer_key.router)

    return app


app = create_app()
