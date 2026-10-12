from fastapi import APIRouter, HTTPException, Request, status

from src.config.settings import get_settings
from src.services import revision_cards as service

router = APIRouter(prefix="/v1/revision", tags=["generation"])


@router.post("/cards", response_model=service.CardsResult)
async def cards(body: service.CardsRequest, request: Request) -> service.CardsResult:
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPENAI_API_KEY is not configured.")
    try:
        return await service.write_cards(llm, body, model=get_settings().verify_model)
    except service.RevisionCardsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not write usable revision cards.") from exc
