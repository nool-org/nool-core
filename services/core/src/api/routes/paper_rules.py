from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes.question_paper import _get_paper_to_edit
from src.api.schemas.question_paper import RulesCheckOut
from src.domain.models import Feature, Role, User
from src.services.paper_rules import rules_check

router = APIRouter(prefix="/api/v1", tags=["question-papers"])


@router.get("/question-papers/{paper_id}/rules-check", summary="Live rules check (C10)")
async def get_rules_check(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> RulesCheckOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await rules_check(session, paper)
