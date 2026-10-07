from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes.question_paper import _get_paper_to_edit
from src.api.schemas.paper_answer_key import CreateParallelIn, ParallelOut
from src.domain.models import Feature, Role, User
from src.services import paper_parallel

router = APIRouter(prefix="/api/v1", tags=["question-papers"])


@router.post("/question-papers/{paper_id}/parallel", status_code=201, summary="Set B (new DRAFT paper)")
async def create_parallel(
    paper_id: str,
    body: CreateParallelIn | None = None,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ParallelOut:
    original = await _get_paper_to_edit(session, paper_id, user)
    return await paper_parallel.create_parallel(session, original, user, body.name if body else None)
