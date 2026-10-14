"""GET /challenges/report - a teacher's report on one of their challenges
(src/services/challenge_report.py): topics, skills, questions, students,
next steps. Only for the teacher's own class and subject."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ValidationError
from src.api.deps import get_db_session, require_role
from src.api.schemas.challenge_report import ChallengeReportOut
from src.domain.models import Role, User
from src.repositories.lookups import get_test_for_teacher
from src.services import challenge_report, quiz_tests

router = APIRouter(prefix="/api/v1", tags=["challenge-report"])


@router.get("/challenges/report", summary="One challenge's report for its teacher")
async def get_challenge_report(
    test_id: str | None = Query(default=None, alias="testId"),
    quiz_test_id: str | None = Query(default=None, alias="quizTestId"),
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ChallengeReportOut:
    if (test_id is None) == (quiz_test_id is None):
        raise ValidationError("Give exactly one of testId or quizTestId.")
    if test_id is not None:
        report = await challenge_report.voice_report(session, await get_test_for_teacher(session, test_id, user))
    else:
        report = await challenge_report.quiz_report(session, await quiz_tests.own_test(session, user, quiz_test_id))
    return ChallengeReportOut(**report)
