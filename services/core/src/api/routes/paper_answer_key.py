from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes.question_paper import _get_paper_to_edit
from src.api.schemas.paper_answer_key import AnswerKeyItemOut, AnswerKeyOut, UpdateAnswerKeyIn
from src.domain.models import Feature, Role, User
from src.services import paper_answer_key

router = APIRouter(prefix="/api/v1", tags=["question-papers"])


@router.get("/question-papers/{paper_id}/answer-key")
async def get_answer_key(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> AnswerKeyOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await paper_answer_key.get_answer_key(session, paper)


@router.post("/question-papers/{paper_id}/answer-key/generate",
             summary="Generate missing keys (all=true: AI-written ones too; overwrite=true: teacher edits too)")
async def generate_answer_key(
    paper_id: str,
    all: bool = False,
    overwrite: bool = False,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> AnswerKeyOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await paper_answer_key.generate_answer_key(session, paper, regenerate=all, overwrite=overwrite)


@router.patch("/question-papers/{paper_id}/answer-key/{question_id}")
async def update_answer_key(
    paper_id: str,
    question_id: str,
    body: UpdateAnswerKeyIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> AnswerKeyItemOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await paper_answer_key.update_answer_key(session, paper, question_id, body, user)
