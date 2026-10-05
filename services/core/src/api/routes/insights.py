from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.insights import TestInsightsOut
from src.domain.models import Feature, Role, User
from src.services.insights import build_test_insights

router = APIRouter(prefix="/api/v1", tags=["insights"])


@router.get("/me/tests/{test_id}/insights")
async def get_test_insights(
    test_id: str,
    user: User = Depends(require_role(Role.STUDENT)),
    _feature: User = Depends(require_feature(Feature.IMPROVEMENT_ANALYSIS)),
    session: AsyncSession = Depends(get_db_session),
    authorization: Annotated[str | None, Header()] = None,
) -> TestInsightsOut:
    token = authorization.split(" ", 1)[-1].strip() if authorization else None
    return TestInsightsOut(**await build_test_insights(session, test_id=test_id, student_id=user.id, token=token))
