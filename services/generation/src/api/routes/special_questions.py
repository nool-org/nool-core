from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services import special_questions as service

router = APIRouter(prefix="/v1/questions", tags=["generation"])


@router.post("/special", response_model=service.SpecialResult)
async def special(body: service.SpecialRequest, request: Request) -> service.SpecialResult:
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    settings = get_settings()
    return await service.write_questions(llm, body, model=settings.writer_model("special_questions"),
                                         verify_model=settings.verify_model)
