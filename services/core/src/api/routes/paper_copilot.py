"""Paper Copilot (C12): chat (typed / voice / quick action) proposing edits
to a question paper, its history, and explain-this-question. Only the
paper's teacher - same rule as the question_paper routes that change it."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError
from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes.question_paper import _get_paper_to_edit
from src.api.schemas.paper_copilot import ExplainOut, PaperChatHistoryOut, PaperChatIn, PaperChatOut
from src.domain.models import Feature, QuestionPaperQuestion, Role, User
from src.services import paper_copilot

router = APIRouter(prefix="/api/v1", tags=["question-papers"])


@router.post("/question-papers/{paper_id}/chat")
async def chat(
    paper_id: str,
    body: PaperChatIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperChatOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await paper_copilot.chat(session, paper, user, body)


@router.get("/question-papers/{paper_id}/chat")
async def chat_history(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperChatHistoryOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return PaperChatHistoryOut(messages=await paper_copilot.history(session, paper))


@router.post("/question-papers/{paper_id}/questions/{question_id}/explain")
async def explain_question(
    paper_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ExplainOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    question = (await session.execute(select(QuestionPaperQuestion).where(
        QuestionPaperQuestion.id == question_id, QuestionPaperQuestion.paper_id == paper.id))).scalar_one_or_none()
    if question is None:
        raise NotFoundError(f'No question "{question_id}" on this paper.')
    return await paper_copilot.explain(session, paper, user, question)
