from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from shared import llm_gateway
from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing
from shared.observability import configure_observability

from src.api.routes import (gap_fill, health, mcq_explain, paper_answer_key, paper_copilot, quiz_analysis,
                            revision_cards, special_questions, teacher_assistant, voice_prep)
from src.config.settings import get_settings
from src.services import cost
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

    @app.middleware("http")
    async def cost_caller(request, call_next):
        """Binds Core's X-Nool-* caller headers for this request's cost log lines,
        and returns the request's summed spend to Core as X-Nool-Cost - on errors
        too, since a failed request can still have paid for model calls."""
        token = cost.caller.set(cost.caller_of(request.headers, request.url.path))
        gateway_token = llm_gateway.bind(request.headers)  # who each call is for, for the AI gateway
        spend = cost.new_spend()
        spend_token = cost.spent.set(spend)
        try:
            response = await call_next(request)
        except Exception:
            if not spend["calls"]:
                raise
            # already logged by RequestContextMiddleware; same envelope as the shared 500 handler
            response = JSONResponse(status_code=500, content={"error": {
                "code": "INTERNAL_ERROR", "message": "An unexpected error occurred.",
                "request_id": request.headers.get("x-request-id"),
            }})
        finally:
            cost.caller.reset(token)
            llm_gateway.reset(gateway_token)
            cost.spent.reset(spend_token)
        if spend["calls"]:
            response.headers["X-Nool-Cost"] = cost.cost_header(spend)
        return response

    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)
    configure_observability(app, settings)

    app.include_router(health.router)
    app.include_router(gap_fill.router)
    app.include_router(voice_prep.router)
    app.include_router(paper_copilot.router)
    app.include_router(paper_answer_key.router)
    app.include_router(teacher_assistant.router)
    app.include_router(mcq_explain.router)
    app.include_router(revision_cards.router)
    app.include_router(quiz_analysis.router)
    app.include_router(special_questions.router)

    return app


app = create_app()
