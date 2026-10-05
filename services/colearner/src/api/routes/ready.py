from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from shared.types import ReadinessCheck, ReadinessStatus

from src.config.settings import get_settings

router = APIRouter()


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness depends on Firebase being configured (for token
    verification) and OPENAI_API_KEY being set.
    Does not call out to Firebase/OpenAI - only confirms the
    service has what it needs when a request arrives.
    """
    settings = get_settings()
    firebase_configured = bool(settings.firebase_project_id)
    model_keys_configured = bool(settings.openai_api_key)
    checks = [
        ReadinessCheck(
            name="firebase_config",
            ok=firebase_configured,
            detail=None if firebase_configured else "FIREBASE_PROJECT_ID is not set",
        ),
        ReadinessCheck(
            name="model_keys",
            ok=model_keys_configured,
            detail=None if model_keys_configured else "OPENAI_API_KEY is not set",
        ),
    ]
    ready_state = firebase_configured and model_keys_configured
    payload = ReadinessStatus(status="ready" if ready_state else "not_ready", checks=checks)
    status_code = status.HTTP_200_OK if ready_state else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=payload.model_dump())
