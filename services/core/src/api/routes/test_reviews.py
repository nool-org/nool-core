"""Teacher clears a needs_review attempt (GET /tests/{id}/reviews lists them,
in voice_test.py) - see services/regrade.py."""

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.test_reviews import ResolveReviewIn, ReviewClearedOut
from src.domain.models import Feature, Role, User
from src.repositories.lookups import get_test_for_teacher
from src.services import regrade as regrade_service

router = APIRouter(prefix="/api/v1", tags=["voice-tests"])


@router.post("/tests/{test_id}/reviews/{session_id}/regrade", summary="Re-run grading on a needs-review attempt")
async def regrade_review(
    test_id: str,
    session_id: str,
    authorization: str | None = Header(default=None),
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewClearedOut:
    test = await get_test_for_teacher(session, test_id, user)
    token = (authorization or "").split(" ", 1)[-1].strip()
    mastery = await regrade_service.regrade(session, test, session_id, user, token)
    return ReviewClearedOut(session_id=session_id, mastery_percent=mastery)


@router.post("/tests/{test_id}/reviews/{session_id}/resolve", summary="Enter a manual score for a needs-review attempt")
async def resolve_review(
    test_id: str,
    session_id: str,
    body: ResolveReviewIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.VOICE_TEST)),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewClearedOut:
    test = await get_test_for_teacher(session, test_id, user)
    await regrade_service.resolve(session, test, session_id, user, body.mastery_percent, body.note)
    return ReviewClearedOut(session_id=session_id, mastery_percent=body.mastery_percent)
