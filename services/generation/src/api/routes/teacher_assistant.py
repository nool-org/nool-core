from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services import teacher_assistant as service

router = APIRouter(prefix="/v1/teacher-assistant", tags=["generation"])


def _llm(request: Request):
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    return llm


@router.post("/reply", response_model=service.ReplyResult)
async def reply(body: service.ReplyRequest, request: Request) -> service.ReplyResult:
    return await service.reply(_llm(request), body, model=get_settings().verify_model)


@router.post("/summary", response_model=service.SummaryResult)
async def summary(body: service.SummaryRequest, request: Request) -> service.SummaryResult:
    return await service.summary(_llm(request), body, model=get_settings().verify_model)
