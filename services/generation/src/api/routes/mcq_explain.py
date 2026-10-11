from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services import mcq_explain as service

router = APIRouter(prefix="/v1/mcq", tags=["generation"])


@router.post("/explain", response_model=service.ExplainResult)
async def explain(body: service.ExplainRequest, request: Request) -> service.ExplainResult:
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    return await service.explain(llm, body, model=get_settings().verify_model)
