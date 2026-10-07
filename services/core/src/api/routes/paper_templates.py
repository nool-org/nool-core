import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ForbiddenError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.routes import question_paper
from src.api.schemas.common import ListEnvelope
from src.api.schemas.paper_templates import (
    CreatePaperTemplateIn,
    DuplicatePaperTemplateIn,
    PaperTemplateOut,
    TemplateBlueprint,
    UpdatePaperTemplateIn,
    UsePaperTemplateIn,
    UsePaperTemplateOut,
)
from src.domain.models import Chapter, Feature, Role, Subject, User
from src.domain.models.paper_template import QuestionPaperTemplate
from src.services.paper_template_starters import STARTERS, is_starter_id
from src.services.paper_templates import (
    blueprint_body,
    blueprint_json,
    blueprint_view,
    snapshot_blueprint,
    validate_blueprint,
)
from src.services.subject_grades import ensure_subjects_enabled_for_school, ensure_subjects_fit_grade

router = APIRouter(prefix="/api/v1", tags=["question-paper-templates"])

_BLUEPRINT_FIELDS = set(TemplateBlueprint.model_fields)


def _can_edit(t: QuestionPaperTemplate, user: User) -> bool:
    """Its creator, or a school admin for a school-wide one."""
    return t.created_by == user.id or (t.scope == "school" and user.role == Role.SCHOOL_ADMIN)


def _out(t: QuestionPaperTemplate, user: User | None = None) -> PaperTemplateOut:
    can_edit = user is not None and _can_edit(t, user)
    return PaperTemplateOut(**{
        **blueprint_view(t.blueprint),
        # the columns are authoritative for what the list filters on
        "exam_type": t.exam_type, "board": t.board, "grade": t.grade,
        "subject_id": t.subject_id and str(t.subject_id), "total_marks": t.total_marks,
        "duration_minutes": t.duration_minutes,
    }, id=str(t.id), name=t.name, scope=t.scope, created_by=str(t.created_by), created_at=t.created_at,
        can_edit=can_edit, can_delete=can_edit)


def _starter_out(template_id: str) -> PaperTemplateOut:
    name, bp = STARTERS[template_id]
    data = bp.model_dump()
    return PaperTemplateOut(
        **{**data, "bloom_targets": data["bloom_targets"] or [], "chapter_ids": data["chapter_ids"] or []},
        id=template_id, name=name, scope="starter", created_by=None, created_at=None, is_starter=True,
    )


def _visible(user: User):
    """Same school, and school-wide or the caller's own."""
    return (QuestionPaperTemplate.school_id == user.school_id) & or_(
        QuestionPaperTemplate.scope == "school", QuestionPaperTemplate.created_by == user.id)


async def _get_template(session: AsyncSession, template_id: str, user: User) -> QuestionPaperTemplate:
    try:
        key = uuid.UUID(template_id)
    except ValueError:
        key = None
    template = key and (await session.execute(select(QuestionPaperTemplate).where(
        QuestionPaperTemplate.id == key, _visible(user),
    ))).scalar_one_or_none()
    if template is None:
        raise NotFoundError(f'No template with id "{template_id}".')
    return template


def _get_starter(template_id: str) -> tuple[str, TemplateBlueprint]:
    if template_id not in STARTERS:
        raise NotFoundError(f'No template with id "{template_id}".')
    return STARTERS[template_id]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check_name(name: str | None) -> str:
    if name is None or not name.strip():
        raise ValidationError("name is required.")
    return name.strip()


def _check_scope(scope: str | None, user: User) -> None:
    if scope == "school" and user.role != Role.SCHOOL_ADMIN:
        raise ForbiddenError("Only a school admin can share a template school-wide.")


async def _check_subject(session: AsyncSession, user: User, bp: TemplateBlueprint) -> None:
    """A subject (when set) is one the school has, of the template's grade;
    chapters belong to it."""
    if bp.subject_id is None:
        if bp.chapter_ids:
            raise ValidationError("chapterIds need a subjectId.")
        return
    try:
        subject = await session.get(Subject, uuid.UUID(bp.subject_id))
    except ValueError:
        subject = None
    if subject is None:
        raise ValidationError(f'No subject with id "{bp.subject_id}".')
    await ensure_subjects_enabled_for_school(session, [bp.subject_id], user.school_id)
    await ensure_subjects_fit_grade(session, [bp.subject_id], bp.grade, what=f"a grade-{bp.grade} template")
    chapter_ids = set(bp.chapter_ids or [])
    if chapter_ids:
        try:
            keys = [uuid.UUID(c) for c in chapter_ids]
        except ValueError:
            raise ValidationError("Every chapter must belong to the template's subject.") from None
        found = {str(i) for (i,) in (await session.execute(
            select(Chapter.id).where(Chapter.id.in_(keys), Chapter.subject_id == subject.id))).all()}
        if chapter_ids - found:
            raise ValidationError("Every chapter must belong to the template's subject.")


def _new_row(user: User, name: str, scope: str, bp: TemplateBlueprint, blueprint: dict) -> QuestionPaperTemplate:
    return QuestionPaperTemplate(
        school_id=user.school_id, created_by=user.id, name=name, scope=scope, exam_type=bp.exam_type,
        board=bp.board, grade=bp.grade, subject_id=bp.subject_id, total_marks=bp.total_marks,
        duration_minutes=bp.duration_minutes, blueprint=blueprint,
    )


def _sync_columns(t: QuestionPaperTemplate, bp: TemplateBlueprint) -> None:
    t.exam_type, t.board, t.grade, t.subject_id = bp.exam_type, bp.board, bp.grade, bp.subject_id
    t.total_marks, t.duration_minutes = bp.total_marks, bp.duration_minutes


async def _save(session: AsyncSession, template: QuestionPaperTemplate, user: User) -> PaperTemplateOut:
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return _out(template, user)


@router.post("/question-paper-templates", status_code=201,
             summary="Save a paper's blueprint as a template, or design one")
async def create_template(
    body: CreatePaperTemplateIn,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperTemplateOut:
    name = _check_name(body.name)
    scope = body.scope or "personal"
    _check_scope(scope, user)
    if body.from_paper_id is not None:
        paper = await question_paper._get_paper_to_edit(session, body.from_paper_id, user)
        blueprint = snapshot_blueprint(await question_paper._serialize(session, paper))
        template = QuestionPaperTemplate(
            school_id=user.school_id, created_by=user.id, name=name, scope=scope, exam_type=paper.exam_type,
            board=paper.board, grade=paper.grade, subject_id=paper.subject_id, total_marks=paper.total_marks,
            duration_minutes=paper.duration_minutes, blueprint=blueprint,
        )
        return await _save(session, template, user)

    bp = validate_blueprint(body.model_dump(include=_BLUEPRINT_FIELDS, exclude_unset=True))
    await _check_subject(session, user, bp)
    return await _save(session, _new_row(user, name, scope, bp, blueprint_json(bp)), user)


@router.get("/question-paper-templates")
async def list_templates(
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
    scope: Literal["personal", "school", "starter"] | None = None,
    board: str | None = None,
    grade: int | None = None,
    exam_type: Annotated[str | None, Query(alias="examType")] = None,
    q: str | None = None,
) -> ListEnvelope[PaperTemplateOut]:
    """personal = mine, school = school-wide in my school, starter = built-in;
    no scope -> all three (saved ones newest first, then the starters)."""
    items: list[PaperTemplateOut] = []
    if scope != "starter":
        query = select(QuestionPaperTemplate).where(_visible(user))
        if scope == "personal":
            query = query.where(QuestionPaperTemplate.scope == "personal")
        elif scope == "school":
            query = query.where(QuestionPaperTemplate.scope == "school")
        if board:
            query = query.where(QuestionPaperTemplate.board == board)
        if grade is not None:
            query = query.where(QuestionPaperTemplate.grade == grade)
        if exam_type:
            query = query.where(QuestionPaperTemplate.exam_type == exam_type)
        if q:
            query = query.where(QuestionPaperTemplate.name.ilike(f"%{_escape_like(q)}%", escape="\\"))
        rows = (await session.execute(query.order_by(QuestionPaperTemplate.created_at.desc()))).scalars().all()
        items = [_out(t, user) for t in rows]
    if scope in (None, "starter"):
        items += [t for t in map(_starter_out, STARTERS) if
                  (not board or t.board == board) and (grade is None or t.grade == grade)
                  and (not exam_type or t.exam_type == exam_type) and (not q or q.lower() in t.name.lower())]
    return ListEnvelope(items=items, total=len(items))


@router.get("/question-paper-templates/{template_id}")
async def get_template(
    template_id: str,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperTemplateOut:
    if is_starter_id(template_id):
        _get_starter(template_id)
        return _starter_out(template_id)
    return _out(await _get_template(session, template_id, user), user)


@router.patch("/question-paper-templates/{template_id}")
async def update_template(
    template_id: str,
    body: UpdatePaperTemplateIn,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperTemplateOut:
    if is_starter_id(template_id):
        _get_starter(template_id)
        raise ForbiddenError("Starter templates are read-only - duplicate one to edit it.")
    template = await _get_template(session, template_id, user)
    if not _can_edit(template, user):
        raise ForbiddenError("Only the template's creator or a school admin can change it.")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes:
        template.name = _check_name(changes["name"])
    if changes.get("scope") is not None and changes["scope"] != template.scope:
        _check_scope(changes["scope"], user)
        template.scope = changes["scope"]
    blueprint_changes = {k: v for k, v in changes.items() if k in _BLUEPRINT_FIELDS}
    if blueprint_changes:
        bp = validate_blueprint({**blueprint_view(template.blueprint), **blueprint_changes},
                                set(blueprint_changes))
        await _check_subject(session, user, bp)
        template.blueprint = blueprint_json(bp, template.blueprint)
        _sync_columns(template, bp)
    return await _save(session, template, user)


@router.post("/question-paper-templates/{template_id}/duplicate", status_code=201,
             summary="Personal copy of a template (starters too)")
async def duplicate_template(
    template_id: str,
    body: DuplicatePaperTemplateIn,
    user: User = Depends(require_role(Role.TEACHER, Role.SCHOOL_ADMIN)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> PaperTemplateOut:
    if is_starter_id(template_id):
        source_name, bp = _get_starter(template_id)
        template = _new_row(user, "", "personal", bp, blueprint_json(bp))
    else:
        source = await _get_template(session, template_id, user)
        source_name = source.name
        template = QuestionPaperTemplate(
            school_id=user.school_id, created_by=user.id, name="", scope="personal",
            exam_type=source.exam_type, board=source.board, grade=source.grade, subject_id=source.subject_id,
            total_marks=source.total_marks, duration_minutes=source.duration_minutes,
            blueprint=dict(source.blueprint),
        )
    template.name = _check_name(body.name) if body.name is not None else f"{source_name} (copy)"
    return await _save(session, template, user)


@router.post("/question-paper-templates/{template_id}/use", status_code=201,
             summary="New DRAFT paper from a template")
async def use_template(
    template_id: str,
    body: UsePaperTemplateIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> UsePaperTemplateOut:
    if is_starter_id(template_id):
        blueprint = blueprint_json(_get_starter(template_id)[1])
    else:
        blueprint = (await _get_template(session, template_id, user)).blueprint
    paper_body = blueprint_body(blueprint, _check_name(body.name), body.chapter_ids, body.topic_ids,
                                body.subject_id)
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
    if is_starter_id(template_id):
        _get_starter(template_id)
        raise ForbiddenError("Starter templates are built in and can't be deleted.")
    template = await _get_template(session, template_id, user)
    if not _can_edit(template, user):
        raise ForbiddenError("Only the template's creator or a school admin can delete it.")
    await session.delete(template)
    await session.commit()
    return Response(status_code=204)
