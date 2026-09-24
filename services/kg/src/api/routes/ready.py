from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from shared.types import ReadinessCheck, ReadinessStatus

from src.services.nool_kg_client import check_connection

router = APIRouter()


@router.get("/ready")
async def ready() -> JSONResponse:
    kg_ok = await check_connection()
    checks = [
        ReadinessCheck(
            name="nool_kg",
            ok=kg_ok,
            detail=None if kg_ok else "unable to reach nool-kg",
        )
    ]
    payload = ReadinessStatus(status="ready" if kg_ok else "not_ready", checks=checks)
    status_code = status.HTTP_200_OK if kg_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=payload.model_dump())
