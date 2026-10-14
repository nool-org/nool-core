from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing
from shared.observability import configure_observability

from src.api.routes import curriculum, health, question_paper, ready, voice_test
from src.config.settings import get_settings
from shared import llm_gateway
from src.services import cost
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

    @app.middleware("http")
    async def cost_header(request, call_next):
        """Returns this request's model spend to Core as X-Nool-Cost (services/cost.py)."""
        spend = cost.new_spend()
        token = cost.spent.set(spend)
        gateway_token = llm_gateway.bind(request.headers)  # who each call is for, for the AI gateway
        try:
            response = await call_next(request)
        finally:
            cost.spent.reset(token)
            llm_gateway.reset(gateway_token)
        if spend["calls"]:
            response.headers["X-Nool-Cost"] = cost.cost_header(spend)
        return response
    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)
    configure_observability(app, settings)

    app.include_router(health.router)
    app.include_router(ready.router)
    app.include_router(curriculum.router)
    app.include_router(question_paper.router)
    app.include_router(voice_test.router)

    return app


app = create_app()
