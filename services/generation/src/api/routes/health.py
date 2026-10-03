from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from shared.types import HealthStatus, ReadinessCheck, ReadinessStatus

router = APIRouter()


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Process health only - no dependency checks. See /ready for those."""
    return HealthStatus()


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    ok = request.app.state.llm is not None
    payload = ReadinessStatus(
        status="ready" if ok else "not_ready",
        checks=[ReadinessCheck(name="llm", ok=ok, detail=None if ok else "OPENAI_API_KEY is not configured")],
    )
    return JSONResponse(status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
                        content=payload.model_dump())
