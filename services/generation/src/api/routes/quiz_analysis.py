from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services import quiz_analysis as service

router = APIRouter(prefix="/v1/quiz", tags=["generation"])


@router.post("/analysis", response_model=service.AnalysisResult)
async def analysis(body: service.AnalysisRequest, request: Request) -> service.AnalysisResult:
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    return await service.analyse(llm, body, model=get_settings().verify_model)
