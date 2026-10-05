from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services.voice_prep import PrepRequest, PrepResult, prep_questions

router = APIRouter(prefix="/v1", tags=["generation"])


@router.post("/voice-test/prep", response_model=PrepResult)
async def voice_test_prep(body: PrepRequest, request: Request) -> PrepResult:
    """One spoken-ready question (+ rubric) per requested Bloom level, in
    order - see src/services/voice_prep.py. An item that fails verification
    twice keeps its original wording (reworded_text == original_text)."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    settings = get_settings()
    return await prep_questions(
        llm, body, generation_model=settings.generation_model, verify_model=settings.verify_model,
        timeout=settings.request_timeout_seconds,
    )
