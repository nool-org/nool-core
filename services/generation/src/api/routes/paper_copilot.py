from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.config.settings import get_settings
from src.services.paper_plan import PlanRequest, PlanResult, plan_paper
from src.services.transcribe import OpenAITranscriber, TranscribeRequest, TranscribeResult, get_transcriber

router = APIRouter(prefix="/v1", tags=["generation"])


@router.post("/paper/plan", response_model=PlanResult)
async def paper_plan(body: PlanRequest, request: Request) -> PlanResult:
    """Paper Copilot: reply + up to 10 structured edit ops - see src/services/paper_plan.py."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    settings = get_settings()
    return await plan_paper(llm, body, plan_model=settings.verify_model, write_model=settings.generation_model)


@router.post("/transcribe", response_model=TranscribeResult)
async def transcribe(
    body: TranscribeRequest, transcriber: OpenAITranscriber | None = Depends(get_transcriber)
) -> TranscribeResult:
    if transcriber is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    try:
        audio = body.audio()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    text = await transcriber(audio=audio, filename=body.filename(), prompt=body.prompt)
    return TranscribeResult(text=text.strip())
