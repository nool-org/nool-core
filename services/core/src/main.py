from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.auth import configure_identity_provider
from shared.auth.providers.firebase import FirebaseIdentityProvider
from shared.errors import register_exception_handlers
from shared.logging import RequestContextMiddleware, configure_logging, get_logger
from shared.tracing import configure_tracing
from shared.observability import configure_observability

from src.api.routes import (
    school_question_bank,
    admin,
    admin_catalog,
    ai_assessor,
    assigned_test,
    assistant,
    bloom_result,
    curriculum,
    dataset,
    devices,
    gamification,
    health,
    homework,
    insights,
    journey,
    leaderboard,
    mcq_test,
    practice_bank,
    profile,
    progress,
    question_paper,
    paper_rules,
    paper_versions,
    ready,
    realtime,
    roster,
    school_admin,
    school_oversight,
    student_dashboard,
    student_homework,
    support_ticket,
    teacher_dashboard,
    test_result,
    voice_test,
)
from src.api.routes import test_reviews
from src.config.settings import get_settings
from src.repositories.database import dispose_engine, init_engine


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level)
    logger = get_logger(__name__)

    # Registered once per process, not per-request - see
    # services/auth/src/services/token_service.py's docstring for why a
    # fresh FirebaseIdentityProvider on every request is unsafe
    # (firebase_admin's app initialization is process-global).
    configure_identity_provider(
        FirebaseIdentityProvider(settings.firebase_project_id, settings.firebase_credentials_path)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_engine(settings.database_url)
        logger.info("startup_complete")
        yield
        await dispose_engine()
        logger.info("shutdown_complete")

    app = FastAPI(title="nool-core Core Service", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    configure_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)
    configure_observability(app, settings)

    app.include_router(health.router)
    app.include_router(ready.router)

    # Foundation
    app.include_router(profile.router)
    app.include_router(devices.router)
    # Teacher
    app.include_router(roster.router)
    app.include_router(curriculum.router)
    app.include_router(dataset.router)
    app.include_router(voice_test.router)
    app.include_router(test_reviews.router)
    app.include_router(test_result.router)
    app.include_router(homework.router)
    app.include_router(question_paper.router)
    app.include_router(paper_rules.router)
    app.include_router(paper_versions.router)
    app.include_router(teacher_dashboard.router)
    app.include_router(assistant.router)
    # Student
    app.include_router(student_dashboard.router)
    app.include_router(assigned_test.router)
    app.include_router(ai_assessor.router)
    app.include_router(student_homework.router)
    app.include_router(practice_bank.router)
    app.include_router(bloom_result.router)
    app.include_router(insights.router)
    app.include_router(progress.router)
    app.include_router(leaderboard.router)
    app.include_router(gamification.router)
    app.include_router(journey.router)
    app.include_router(mcq_test.router)
    app.include_router(realtime.router)
    # Super Admin / School Admin
    app.include_router(admin.router)
    app.include_router(admin_catalog.router)
    app.include_router(school_admin.router)
    app.include_router(school_oversight.router)
    app.include_router(school_question_bank.router)
    app.include_router(support_ticket.router)

    return app


app = create_app()
