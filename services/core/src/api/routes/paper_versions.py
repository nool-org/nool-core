from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes.question_paper import _get_paper_to_edit
from src.api.schemas.question_paper import PaperActionsIn, PaperActionsOut, PaperVersionOut, PaperVersionsOut
from src.domain.models import Feature, Role, User
from src.domain.models.question_paper import QuestionPaperVersion
from src.services.paper_actions import apply_ops, restore_version

router = APIRouter(prefix="/api/v1", tags=["question-papers"])


@router.post("/question-papers/{paper_id}/actions", summary="Apply structured ops (C11) as a new version")
async def post_actions(
    paper_id: str,
    body: PaperActionsIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperActionsOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    out = await apply_ops(session, paper, body.ops, created_by=user.id, summary=body.summary, origin=body.origin)
    await session.commit()
    return out


@router.get("/question-papers/{paper_id}/versions")
async def list_versions(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperVersionsOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    rows = (await session.execute(
        select(QuestionPaperVersion).where(QuestionPaperVersion.paper_id == paper.id)
        .order_by(QuestionPaperVersion.number.desc())
    )).scalars().all()
    return PaperVersionsOut(items=[
        PaperVersionOut(id=str(v.id), number=v.number, summary=v.summary, origin=v.origin,
                        created_at=v.created_at, created_by=str(v.created_by))
        for v in rows
    ])


@router.post("/question-papers/{paper_id}/versions/{version_id}/restore", summary="Restore as a NEW version")
async def restore(
    paper_id: str,
    version_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperActionsOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    out = await restore_version(session, paper, version_id, created_by=user.id)
    await session.commit()
    return out
