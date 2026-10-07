from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services.paper_answer_key import AnswerKeyRequest, AnswerKeyResult, answer_key

router = APIRouter(prefix="/v1", tags=["generation"])


@router.post("/paper/answer-key", response_model=AnswerKeyResult)
async def paper_answer_key(body: AnswerKeyRequest, request: Request) -> AnswerKeyResult:
    """Answer + step marking per question - see src/services/paper_answer_key.py.
    Items that fail verification twice come back in `failed_ids`."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    settings = get_settings()
    return await answer_key(llm, body, generation_model=settings.generation_model, verify_model=settings.verify_model)
