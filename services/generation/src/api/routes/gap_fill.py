from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services.gap_fill import GapFillRequest, GapFillResult, fill_gaps

router = APIRouter(prefix="/v1", tags=["generation"])


@router.post("/homework/gap-fill", response_model=GapFillResult)
async def gap_fill(body: GapFillRequest, request: Request) -> GapFillResult:
    """Questions for the homework slots the question bank couldn't fill -
    see src/services/gap_fill.py. Slots that fail verification twice come
    back in `unfilled_slot_indexes`, never as an unverified question."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    settings = get_settings()
    return await fill_gaps(
        llm, body, generation_model=settings.writer_model("gap_fill"), verify_model=settings.verify_model,
        timeout=settings.request_timeout_seconds,
    )
