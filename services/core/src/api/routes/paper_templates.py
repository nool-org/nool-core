from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ForbiddenError, NotFoundError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes import question_paper
from src.api.schemas.common import ListEnvelope
from src.api.schemas.paper_templates import (
    CreatePaperTemplateIn,
    PaperTemplateOut,
    UsePaperTemplateIn,
    UsePaperTemplateOut,
)
from src.domain.models import Feature, Role, User
from src.domain.models.paper_template import QuestionPaperTemplate
from src.services.paper_templates import blueprint_body, snapshot_blueprint

router = APIRouter(prefix="/api/v1", tags=["question-paper-templates"])


def _out(t: QuestionPaperTemplate, user: User | None = None) -> PaperTemplateOut:
    return PaperTemplateOut(
        id=str(t.id), name=t.name, exam_type=t.exam_type, board=t.board, grade=t.grade,
        subject_id=str(t.subject_id), total_marks=t.total_marks, duration_minutes=t.duration_minutes,
        created_by=str(t.created_by), created_at=t.created_at,
        can_delete=user is not None and (user.role == Role.SCHOOL_ADMIN or t.created_by == user.id),
    )


async def _get_template(session: AsyncSession, template_id: str, user: User) -> QuestionPaperTemplate:
    template = (await session.execute(select(QuestionPaperTemplate).where(
        QuestionPaperTemplate.id == template_id, QuestionPaperTemplate.school_id == user.school_id,
    ))).scalar_one_or_none()
    if template is None:
        raise NotFoundError(f'No template with id "{template_id}".')
    return template


@router.post("/question-paper-templates", status_code=201, summary="Save a paper's blueprint as a template")
async def create_template(
    body: CreatePaperTemplateIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperTemplateOut:
    paper = await question_paper._get_paper_to_edit(session, body.from_paper_id, user)
    blueprint = snapshot_blueprint(await question_paper._serialize(session, paper))
    template = QuestionPaperTemplate(
        school_id=user.school_id, created_by=user.id, name=body.name, exam_type=paper.exam_type,
        board=paper.board, grade=paper.grade, subject_id=paper.subject_id, total_marks=paper.total_marks,
        duration_minutes=paper.duration_minutes, blueprint=blueprint,
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return _out(template, user)


@router.get("/question-paper-templates")
async def list_templates(
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[PaperTemplateOut]:
    rows = (await session.execute(
        select(QuestionPaperTemplate).where(QuestionPaperTemplate.school_id == user.school_id)
        .order_by(QuestionPaperTemplate.created_at.desc())
    )).scalars().all()
    return ListEnvelope(items=[_out(t, user) for t in rows], total=len(rows))


@router.post("/question-paper-templates/{template_id}/use", status_code=201,
             summary="New DRAFT paper from a template")
async def use_template(
    template_id: str,
    body: UsePaperTemplateIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> UsePaperTemplateOut:
    template = await _get_template(session, template_id, user)
    paper_body = blueprint_body(template.blueprint, body.name, body.chapter_ids, body.topic_ids)
    # same path as POST /question-papers: plan limit, blueprint checks, DRAFT
    paper = await question_paper.create_paper(paper_body, user=user, _feature=user, session=session)
    return UsePaperTemplateOut(paper_id=paper.id)


@router.delete("/question-paper-templates/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    template = await _get_template(session, template_id, user)
    if user.role != Role.SCHOOL_ADMIN and template.created_by != user.id:
        raise ForbiddenError("Only the template's creator or a school admin can delete it.")
    await session.delete(template)
    await session.commit()
    return Response(status_code=204)
