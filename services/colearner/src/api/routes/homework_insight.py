from fastapi import APIRouter, Depends

from shared.auth import AuthenticatedUser

from src.api.deps import get_current_user
from src.domain.schemas import HomeworkInsightRequest, HomeworkInsightResponse
from src.services.homework_insight_service import generate_homework_insight

router = APIRouter(prefix="/api/v1", tags=["homework-insight"])


@router.post("/homework-insight", response_model=HomeworkInsightResponse)
async def create_homework_insight(
    payload: HomeworkInsightRequest,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> HomeworkInsightResponse:
    """Stateless - see HomeworkInsightRequest's doc comment. Core calls
    this once per Homework (see student_homework.py's get_homework_context)
    and persists the result, rather than regenerating on every screen
    view.
    """
    result = await generate_homework_insight(
        topic=payload.topic,
        grade=payload.grade,
        subject=payload.subject,
        mastery_percent=payload.mastery_percent,
        weak_bloom_level=payload.weak_bloom_level,
        textbook_context=payload.textbook_context,
    )
    return HomeworkInsightResponse(**result)
