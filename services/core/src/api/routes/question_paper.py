import difflib
import logging
import time
import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer_group

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.bloom import BloomTarget
from src.api.schemas.common import ListEnvelope
from src.api.schemas.dataset import DatasetShare
from src.api.schemas.question_paper import (
    BloomTargetPercent,
    ChapterMarks,
    CreateQuestionPaperIn,
    DifficultyMix,
    GeneratePaperIn,
    PaperDifficultyTarget,
    PaperRules,
    PaperSection,
    PaperValidationOut,
    QuestionPaperOut,
    QuestionPaperQuestionCandidateOut,
    QuestionPaperQuestionOut,
    ReorderQuestionsIn,
    RenamePaperIn,
    UpdateQuestionPaperQuestionIn,
)
from src.domain.models import (
    BloomLevel,
    Chapter,
    Dataset,
    DatasetQuestion,
    DatasetType,
    Feature,
    PaperDifficultyLevel,
    QuestionPaper,
    QuestionPaperBloomDistribution,
    QuestionPaperChapter,
    QuestionPaperDatasetShare,
    QuestionPaperDifficultyDistribution,
    QuestionPaperQuestion,
    QuestionPaperSection,
    QuestionPaperStatus,
    QuestionPaperTopic,
    QuestionPaperValidation,
    QuestionType,
    Role,
    School,
    SchoolClass,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
)
from src.domain.models.question_paper import QuestionSource
from src.repositories.dataset_access import ensure_datasets_visible, visible_datasets
from src.services.subject_grades import ensure_subjects_enabled_for_school, ensure_subjects_fit_grade
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import count_question_papers
from src.services import kg_client
from src.services.kg_client import KgServiceUnavailableError
from src.services.bloom_defaults import resolve_bloom_targets
from src.services.paper_actions import COPILOT_COLUMNS, create_version, load_questions, question_out
from src.services.paper_instructions import sync_instructions
from src.services.paper_repeats import normalize
from src.services.question_selector import (
    concept_full, crowded, duplicates_of, topic_full, figure_svgs, has_figure, over_marked, plain_recall, same_question, text_only_filter, unfit_for_paper,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["question-papers"])


async def _get_paper_in_school(session: AsyncSession, paper_id: str, school_id) -> QuestionPaper:
    result = await session.execute(
        select(QuestionPaper).where(QuestionPaper.id == paper_id, QuestionPaper.school_id == school_id)
    )
    paper = result.scalar_one_or_none()
    if paper is None:
        raise NotFoundError(f'No Question Paper with id "{paper_id}".')
    return paper


async def _serialize(session: AsyncSession, paper: QuestionPaper) -> QuestionPaperOut:
    shares = await session.execute(
        select(QuestionPaperDatasetShare).where(QuestionPaperDatasetShare.paper_id == paper.id)
    )
    chapters = (await session.execute(
        select(QuestionPaperChapter.chapter_id, QuestionPaperChapter.marks).where(QuestionPaperChapter.paper_id == paper.id)
    )).all()
    topics = await session.execute(
        select(QuestionPaperTopic.topic_id).where(QuestionPaperTopic.paper_id == paper.id)
    )
    sections = list((await session.execute(
        select(QuestionPaperSection).where(QuestionPaperSection.paper_id == paper.id)
    )).scalars().all())
    bloom_dist = await session.execute(
        select(QuestionPaperBloomDistribution).where(QuestionPaperBloomDistribution.paper_id == paper.id)
    )
    difficulty_dist = await session.execute(
        select(QuestionPaperDifficultyDistribution).where(
            QuestionPaperDifficultyDistribution.paper_id == paper.id
        )
    )
    validation_row = await session.execute(
        select(QuestionPaperValidation).where(QuestionPaperValidation.paper_id == paper.id)
    )
    validation = validation_row.scalar_one_or_none()
    bloom_rows = bloom_dist.scalars().all()
    difficulty_rows = {d.level: d.value for d in difficulty_dist.scalars().all()}

    return QuestionPaperOut(
        created_at=paper.created_at,
        id=str(paper.id),
        name=paper.name,
        exam_type=paper.exam_type,
        board=paper.board,
        grade=paper.grade,
        language=paper.language,
        subject_id=str(paper.subject_id),
        dataset_shares=[
            DatasetShare(dataset_id=str(s.dataset_id), percent=s.percent) for s in shares.scalars().all()
        ],
        chapter_ids=[str(row[0]) for row in chapters],
        chapter_marks=[ChapterMarks(chapter_id=str(c), marks=m) for c, m in chapters if m is not None],
        topic_ids=[str(row[0]) for row in topics.all()],
        total_marks=paper.total_marks,
        duration_minutes=paper.duration_minutes,
        sections=[
            PaperSection(label=s.label, question_count=s.question_count, marks_each=s.marks_each,
                         question_types=s.question_types)
            for s in sections
        ],
        bloom_distribution=[BloomTarget(level=d.bloom_level, value=d.value) for d in bloom_rows],
        difficulty_distribution=[PaperDifficultyTarget(level=k, value=v) for k, v in difficulty_rows.items()],
        bloom_targets=[BloomTargetPercent(level=d.bloom_level, percent=d.value) for d in bloom_rows] or None,
        difficulty_mix=DifficultyMix(
            easy=difficulty_rows.get(PaperDifficultyLevel.EASY, 0),
            medium=difficulty_rows.get(PaperDifficultyLevel.MEDIUM, 0),
            hard=difficulty_rows.get(PaperDifficultyLevel.HARD, 0),
        ) if difficulty_rows else None,
        question_types=paper.question_types or [],
        rules=PaperRules(
            allow_internal_choice=paper.allow_internal_choice,
            avoid_duplicate_concepts=paper.avoid_duplicate_concepts,
            respect_chapter_weightage=paper.respect_chapter_weightage,
            avoid_recent_repetition=paper.avoid_recent_repetition,
        ),
        status=paper.status,
        validation=(
            PaperValidationOut(
                coverage_percent=validation.coverage_percent,
                marks_accounted_for=validation.marks_accounted_for,
                duplicate_count=validation.duplicate_count,
                quality_percent=validation.quality_percent,
                issues=validation.issues,
            )
            if validation is not None
            else None
        ),
        custom_header_text=paper.custom_header_text,
        logo_data_uri=paper.logo_data_uri,
        instructions=sync_instructions(paper.instructions, sections),  # T-028: never stale
    )


async def _apply_body(session: AsyncSession, paper: QuestionPaper, body: CreateQuestionPaperIn) -> None:
    paper.name = body.name
    paper.exam_type = body.exam_type
    paper.board = body.board
    paper.grade = body.grade
    paper.language = body.language
    paper.subject_id = body.subject_id
    paper.total_marks = body.total_marks
    paper.duration_minutes = body.duration_minutes
    paper.question_types = body.question_types
    paper.custom_header_text = body.custom_header_text
    paper.logo_data_uri = body.logo_data_uri
    paper.instructions = body.instructions
    paper.status = QuestionPaperStatus.DRAFT

    for model, existing_field in (
        (QuestionPaperDatasetShare, "paper_id"),
        (QuestionPaperChapter, "paper_id"),
        (QuestionPaperTopic, "paper_id"),
        (QuestionPaperSection, "paper_id"),
        (QuestionPaperBloomDistribution, "paper_id"),
        (QuestionPaperDifficultyDistribution, "paper_id"),
    ):
        existing = await session.execute(select(model).where(getattr(model, existing_field) == paper.id))
        for row in existing.scalars().all():
            await session.delete(row)
    await session.flush()

    for share in body.dataset_shares or []:
        session.add(
            QuestionPaperDatasetShare(paper_id=paper.id, dataset_id=share.dataset_id, percent=share.percent)
        )
    weightage = {c.chapter_id: c.marks for c in body.chapter_marks or []}
    for chapter_id in body.chapter_ids or []:
        session.add(QuestionPaperChapter(paper_id=paper.id, chapter_id=chapter_id, marks=weightage.get(chapter_id)))
    for topic_id in body.topic_ids or []:
        session.add(QuestionPaperTopic(paper_id=paper.id, topic_id=topic_id))
    for section in body.sections or []:
        session.add(
            QuestionPaperSection(
                paper_id=paper.id,
                label=section.label,
                question_count=section.question_count,
                marks_each=section.marks_each,
                question_types=section.question_types or None,
            )
        )
    school = await session.get(School, paper.school_id)
    bloom = (
        [BloomTarget(level=t.level, value=t.percent) for t in body.bloom_targets]
        if body.bloom_targets else body.bloom_distribution
    )
    for target in resolve_bloom_targets(school, bloom):
        session.add(
            QuestionPaperBloomDistribution(paper_id=paper.id, bloom_level=target.level, value=target.value)
        )
    difficulty = body.difficulty_distribution or []
    if body.difficulty_mix is not None:
        mix = body.difficulty_mix
        difficulty = [PaperDifficultyTarget(level=level, value=value) for level, value in (
            (PaperDifficultyLevel.EASY, mix.easy), (PaperDifficultyLevel.MEDIUM, mix.medium),
            (PaperDifficultyLevel.HARD, mix.hard))]
    for target in difficulty:
        session.add(
            QuestionPaperDifficultyDistribution(paper_id=paper.id, level=target.level, value=target.value)
        )


async def _ensure_teaches(session: AsyncSession, user: User, subject_id, grade: int) -> None:
    teaches = (await session.execute(
        select(TeacherClassAssignment.id)
        .join(SchoolClass, SchoolClass.id == TeacherClassAssignment.class_id)
        .where(TeacherClassAssignment.teacher_id == user.id, TeacherClassAssignment.subject_id == subject_id,
               SchoolClass.grade == grade, SchoolClass.school_id == user.school_id)
        .limit(1)
    )).first()
    if teaches is None:
        raise ForbiddenError(f"You don't teach this subject in grade {grade}.")


async def _get_paper_to_edit(session: AsyncSession, paper_id: str, user: User) -> QuestionPaper:
    """A paper in the teacher's school that they also teach (subject + grade) -
    reading it and changing it alike: another subject's unreleased paper isn't theirs to see (V-017)."""
    paper = await _get_paper_in_school(session, paper_id, user.school_id)
    await _ensure_teaches(session, user, paper.subject_id, paper.grade)
    return paper


async def _check_blueprint(session: AsyncSession, user: User, body: CreateQuestionPaperIn) -> None:
    """A paper is one subject of one grade, set by a teacher of it: the
    subject is enabled for the school and is that grade's, every chapter
    belongs to it, every topic to a chosen chapter, and every dataset share
    to the same subject + grade (and is one this teacher may use)."""
    await _ensure_teaches(session, user, body.subject_id, body.grade)
    await ensure_subjects_enabled_for_school(session, [body.subject_id], user.school_id)
    await ensure_subjects_fit_grade(session, [body.subject_id], body.grade, what=f"a grade-{body.grade} paper")

    chapter_ids = {str(c) for c in body.chapter_ids or []}
    if chapter_ids:
        found = {str(i) for (i,) in (await session.execute(
            select(Chapter.id).where(Chapter.id.in_(chapter_ids), Chapter.subject_id == body.subject_id))).all()}
        if chapter_ids - found:
            raise ValidationError("Every chapter must belong to the paper's subject.")
    if any(c.chapter_id not in chapter_ids for c in body.chapter_marks or []):
        raise ValidationError("Chapter marks are only for the paper's chapters.")
    topic_ids = {str(t) for t in body.topic_ids or []}
    if topic_ids:
        in_scope = Topic.chapter_id.in_(chapter_ids) if chapter_ids else Topic.chapter_id.in_(
            select(Chapter.id).where(Chapter.subject_id == body.subject_id))
        found = {str(i) for (i,) in (await session.execute(
            select(Topic.id).where(Topic.id.in_(topic_ids), in_scope))).all()}
        if topic_ids - found:
            raise ValidationError("Every topic must belong to one of the paper's chapters.")

    share_ids = [str(share.dataset_id) for share in body.dataset_shares or []]
    if share_ids:
        datasets = (await session.execute(select(Dataset).where(Dataset.id.in_(share_ids)))).scalars().all()
        if any(str(d.subject_id) != str(body.subject_id) or d.grade != body.grade for d in datasets):
            raise ValidationError(f"Every dataset must be for this subject and grade {body.grade}.")
        await ensure_datasets_visible(session, user, share_ids, subject_id=body.subject_id, grade=body.grade)


# CBSE Class 10 unit-wise weightage (theory paper, 80 marks): (unit marks, its chapter numbers).
# A unit's marks are split evenly across its chapters.
_CBSE_10_UNITS = {
    "Mathematics": [(6, [1]), (20, [2, 3, 4, 5]), (6, [7]), (15, [6, 10]), (12, [8, 9]), (10, [11, 12]),
                    (11, [13, 14])],
    "Science": [(25, [1, 2, 3, 4]), (25, [5, 6, 7, 8]), (12, [9, 10]), (13, [11, 12]), (5, [13])],
}


@router.get("/question-papers/chapter-weightage", summary="CBSE board weightage per chapter (tenths of a mark, out of 800)")
async def chapter_weightage(
    subject_id: str = Query(alias="subjectId"),
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> list[ChapterMarks]:
    """Each chapter's share of the CBSE board paper, as relative weights, for the
    paper setup to scale to its own total. Empty for a subject without an official split."""
    try:
        subject = await session.get(Subject, uuid.UUID(subject_id))
    except ValueError:
        subject = None
    units = _CBSE_10_UNITS.get(subject.name) if subject is not None and subject.grade == 10 else None
    if not units:
        return []
    by_number = {n: marks / len(chapters) for marks, chapters in units for n in chapters}
    chapters = (await session.execute(select(Chapter).where(Chapter.subject_id == subject.id))).scalars().all()
    # in tenths of a mark (out of 800), so equal chapters stay equal - the app scales to its own total
    out = {str(c.id): round(by_number[c.order_index] * 10) for c in chapters if c.order_index in by_number}
    return [ChapterMarks(chapter_id=c, marks=m) for c, m in out.items()]


@router.get("/question-papers")
async def list_papers(
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperOut]:
    taught = (select(TeacherClassAssignment.id).join(SchoolClass, SchoolClass.id == TeacherClassAssignment.class_id)
              .where(TeacherClassAssignment.teacher_id == user.id,
                     TeacherClassAssignment.subject_id == QuestionPaper.subject_id,
                     SchoolClass.grade == QuestionPaper.grade, SchoolClass.school_id == user.school_id))
    result = await session.execute(select(QuestionPaper).where(QuestionPaper.school_id == user.school_id, taught.exists())
                                   .order_by(QuestionPaper.created_at.desc()))  # newest first (T-065)
    items = [await _serialize(session, p) for p in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/question-papers/{paper_id}")
async def get_paper(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await _serialize(session, paper)


@router.post("/question-papers", status_code=201, summary="Create (DRAFT)")
async def create_paper(
    body: CreateQuestionPaperIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    plan = await get_active_plan(session, user.school_id)
    if plan is not None and plan.question_paper_limit is not None:
        if await count_question_papers(session, user.school_id) >= plan.question_paper_limit:
            raise ConflictError(f"Your plan allows up to {plan.question_paper_limit} Question Papers.")

    await _check_blueprint(session, user, body)

    # The NOT NULL scalar columns (name/examType/board/grade/language/
    # subjectId) must be set before this flush, not only inside
    # _apply_body afterward - _apply_body needs paper.id already assigned
    # (id has no server-side default, only a Python-side one SQLAlchemy
    # only applies at flush) to attach child rows, so the flush has to
    # come first; leaving these columns unset until then violates their
    # NOT NULL constraint on insert.
    paper = QuestionPaper(
        school_id=user.school_id, created_by=user.id, status=QuestionPaperStatus.DRAFT,
        name=body.name, exam_type=body.exam_type, board=body.board, grade=body.grade,
        language=body.language, subject_id=body.subject_id,
    )
    session.add(paper)
    await session.flush()
    await _apply_body(session, paper, body)
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)


@router.put("/question-papers/{paper_id}", summary="Overwrite blueprint")
async def update_paper(
    paper_id: str,
    body: CreateQuestionPaperIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.status == QuestionPaperStatus.FINALIZED:
        raise ConflictError("This paper is finalized and can't be changed.")
    await _check_blueprint(session, user, body)
    await _apply_body(session, paper, body)
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)


async def _get_paper_owned(session: AsyncSession, paper_id: str, user: User) -> QuestionPaper:
    """Renaming, duplicating and deleting are for the teacher who made the paper (T-067)."""
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.created_by != user.id:
        raise ForbiddenError("Only the teacher who made this paper can do that.")
    return paper


@router.patch("/question-papers/{paper_id}", summary="Rename")
async def rename_paper(
    paper_id: str,
    body: RenamePaperIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_owned(session, paper_id, user)
    paper.name = body.name.strip() or paper.name
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)


@router.delete("/question-papers/{paper_id}", status_code=204, summary="Delete a paper and everything on it")
async def delete_paper(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    paper = await _get_paper_owned(session, paper_id, user)
    # answer keys, versions, chat and Set B links cascade in the database; these children don't
    for child in (QuestionPaperQuestion, QuestionPaperDatasetShare, QuestionPaperChapter, QuestionPaperTopic,
                  QuestionPaperSection, QuestionPaperBloomDistribution, QuestionPaperDifficultyDistribution,
                  QuestionPaperValidation):
        await session.execute(delete(child).where(child.paper_id == paper.id))
    await session.delete(paper)
    await session.commit()


@router.post("/question-papers/{paper_id}/duplicate", status_code=201, summary="Copy a paper, questions included")
async def duplicate_paper(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    original = await _get_paper_owned(session, paper_id, user)
    plan = await get_active_plan(session, user.school_id)
    if plan is not None and plan.question_paper_limit is not None:
        if await count_question_papers(session, user.school_id) >= plan.question_paper_limit:
            raise ConflictError(f"Your plan allows up to {plan.question_paper_limit} Question Papers.")
    blueprint = CreateQuestionPaperIn.model_validate((await _serialize(session, original)).model_dump())
    blueprint.name = f"{original.name} (copy)"[:120]
    questions = await load_questions(session, original.id)
    copy = QuestionPaper(
        school_id=user.school_id, created_by=user.id, status=QuestionPaperStatus.DRAFT, name=blueprint.name,
        exam_type=blueprint.exam_type, board=blueprint.board, grade=blueprint.grade, language=blueprint.language,
        subject_id=blueprint.subject_id,
    )
    session.add(copy)
    await session.flush()
    await _apply_body(session, copy, blueprint)
    for q in questions:
        session.add(QuestionPaperQuestion(
            paper_id=copy.id, order=q.order, bloom_level=q.bloom_level, marks=q.marks, text=q.text,
            question_type=q.question_type, difficulty=q.difficulty, chapter_id=q.chapter_id, topic_id=q.topic_id,
            section_name=q.section_name, options=q.options, answer=q.answer, source=q.source,
            dataset_question_id=q.dataset_question_id))
    if questions:  # an editable copy: in review, never finalized
        copy.status = QuestionPaperStatus.REVIEW
        await create_version(session, copy, created_by=user.id, summary="Copied from " + original.name[:60],
                             origin="manual")
    await session.commit()
    await session.refresh(copy)
    return await _serialize(session, copy)


async def _resolve_kg_scope(session: AsyncSession, paper: QuestionPaper) -> tuple[list[dict], list[str]]:
    """Resolves this paper's selected chapters/topics into what kg_client
    needs: chapters by (KG chapter number, name) and topic titles to filter
    to. A chapter only selected implicitly (via a topic under it, with no
    explicit QuestionPaperChapter row) is still included - the KG needs its
    number to scope the query regardless of how the blueprint UI expressed it.

    Chapters with no `order_index` (never synced from the KG - see
    POST /admin/curriculum/sync-from-kg) have no KG-side number to match on
    and are skipped: there's nothing grounded to generate for them yet.
    """
    chapter_ids = (
        (await session.execute(select(QuestionPaperChapter.chapter_id).where(QuestionPaperChapter.paper_id == paper.id)))
        .scalars()
        .all()
    )
    topic_rows = (
        (
            await session.execute(
                select(Topic).join(QuestionPaperTopic, QuestionPaperTopic.topic_id == Topic.id).where(
                    QuestionPaperTopic.paper_id == paper.id
                )
            )
        )
        .scalars()
        .all()
    )
    all_chapter_ids = set(chapter_ids) | {t.chapter_id for t in topic_rows}
    if not all_chapter_ids:
        return [], []

    chapters = (
        (await session.execute(select(Chapter).where(Chapter.id.in_(all_chapter_ids))))
        .scalars()
        .all()
    )
    chapter_refs = [
        {"number": c.order_index, "name": c.name} for c in chapters if c.order_index is not None
    ]
    topic_names = [t.name for t in topic_rows]
    return chapter_refs, topic_names


async def _paper_kg_identity(session: AsyncSession, paper: QuestionPaper) -> tuple[str, str, int]:
    """(subject name, board, grade) - what the kg service needs to know
    *which* Curriculum root to query. Without these, the kg service
    silently falls back to its own defaults (CBSE/Grade 10/Science)
    regardless of what this paper is actually for - see kg_client.py's
    docstring.

    Prefers the paper's linked Dataset when one names a real KG root: a
    PRIMARY_CONTENT Dataset (e.g. "10th Science") *is* that root - see
    Dataset's/DatasetType's docstrings - so if the paper shares from one
    that has both board and grade set, that dataset is the authoritative
    answer, not the paper's own (denormalized-at-creation) board/grade. A
    QA-type dataset share (a hand-curated question bank) is never treated
    as a KG root, even if it happens to have board/grade set. Falls back
    to the paper's own fields when there's no qualifying dataset share.
    """
    share_result = await session.execute(
        select(Dataset)
        .join(QuestionPaperDatasetShare, QuestionPaperDatasetShare.dataset_id == Dataset.id)
        .where(QuestionPaperDatasetShare.paper_id == paper.id)
        .order_by(QuestionPaperDatasetShare.percent.desc())
    )
    for dataset in share_result.scalars().all():
        if dataset.type == DatasetType.PRIMARY_CONTENT and dataset.board is not None and dataset.grade is not None:
            subject_id = dataset.subject_id or paper.subject_id
            subject = await session.get(Subject, subject_id)
            subject_name = subject.name if subject is not None else "Science"
            return subject_name, dataset.board, dataset.grade

    subject = await session.get(Subject, paper.subject_id)
    subject_name = subject.name if subject is not None else "Science"
    return subject_name, paper.board, paper.grade


_REFERENCE_POOL_SIZE = 40
BANK_SHARE = 0.25  # of a generated paper's questions taken straight from the question bank
DIAGRAM_SHARE, MAX_DIAGRAMS = 0.15, 5  # of them, the questions with a figure (B23): about 15%, never more than 5


def diagram_quota(slot_count: int, requested: int | None) -> int:
    """Figure questions for a paper of `slot_count` questions: `requested` (0 = none, never more than half
    the paper), else about DIAGRAM_SHARE of it."""
    if requested is not None:
        return min(requested, slot_count // 2)
    return min(MAX_DIAGRAMS, round(slot_count * DIAGRAM_SHARE))


async def _bank_shares(session: AsyncSession, paper: QuestionPaper, user: User) -> list[tuple]:
    """(dataset_id, percent) of the paper's QA banks - the ones picked on it,
    else every QA bank the teacher can see for its subject + grade."""
    shares = (
        await session.execute(
            select(QuestionPaperDatasetShare.dataset_id, QuestionPaperDatasetShare.percent)
            .join(Dataset, Dataset.id == QuestionPaperDatasetShare.dataset_id)
            .where(QuestionPaperDatasetShare.paper_id == paper.id, Dataset.type == DatasetType.QA)
        )
    ).all()
    if shares:
        return list(shares)
    return [(d.id, 1) for d in await visible_datasets(session, user, subject_id=paper.subject_id, grade=paper.grade)
            if d.type == DatasetType.QA]


async def _bank_slots(session: AsyncSession, paper: QuestionPaper, user: User, slots: list[dict],
                      diagrams: int | None = None) -> dict[int, dict]:
    """About BANK_SHARE of the slots filled straight from the question bank
    (same bloom/type/difficulty/marks), spread across the paper. `diagram_quota` of them are questions
    with a figure (they count towards that share); all the others are plain text."""
    from src.services.paper_parallel import _scope_chapter_ids  # paper_parallel imports this module

    dataset_ids = [d for d, _ in await _bank_shares(session, paper, user)]
    chapter_ids = await _scope_chapter_ids(session, paper)
    if not dataset_ids or not chapter_ids or len(slots) < 2:
        return {}
    figures = diagram_quota(len(slots), diagrams)
    quota, step = max(1, round(len(slots) * BANK_SHARE), figures), max(1, round(1 / BANK_SHARE))
    order = [i for start in range(step) for i in range(start, len(slots), step)]  # 0,4,8,..,1,5,9,..
    picked: dict[int, dict] = {}
    taken: list[str] = []
    for want_figure in (True, False):  # the figure questions first, then plain ones up to the share
        # C-020: a figure question is rare, so it may differ in difficulty, then Bloom level, from its slot
        for relax in ((0, 1, 2) if want_figure else (0,)):
            for i in order:
                if len(picked) == (figures if want_figure else quota):
                    break
                if i not in picked:
                    await _pick_bank_slot(session, slots, i, picked, taken, dataset_ids, chapter_ids, want_figure, relax)
    return picked


async def _pick_bank_slot(session, slots, i, picked, taken, dataset_ids, chapter_ids, figure: bool, relax: int = 0, others=()) -> None:
    from src.services.paper_parallel import _bank_pick

    slot = slots[i]
    want = SimpleNamespace(bloom_level=BloomLevel(slot["bloom_level"]), marks=slot["marks"],
                           question_type=_question_type(slot["question_type"]), difficulty=slot["difficulty"],
                           chapter_id=slot.get("chapter_id"))
    bank = await _bank_pick(session, want, dataset_ids=dataset_ids, chapter_ids=chapter_ids, taken=taken,
                            figures=figure, relax=relax, total=len(slots),
                            avoid_topics={t for t in (v.get("topic_id") for v in picked.values())
                                          if topic_full(t, [v.get("topic_id") for v in picked.values()], len(slots))},
                            related=[*others, *((v['text'], v.get('options'), v.get('answer')) for v in picked.values())])
    if bank is None:
        return
    taken.append(bank.text)
    picked[i] = {"text": bank.text, "answer": bank.answer, "options": bank.options, "bank_id": bank.id,
                 "bloom_level": bank.bloom_level.value,  # its own label: a relaxed pick may differ from the slot's (T-028)
                 "difficulty": bank.difficulty.value if bank.difficulty else slot["difficulty"],
                 "question_type": (bank.question_type.value if bank.question_type else slot["question_type"]),
                 "chapter_id": bank.chapter_id, "topic_id": bank.topic_id,
                 "chapter_number": (await session.get(Chapter, bank.chapter_id)).order_index}


async def _dataset_reference_questions(session: AsyncSession, paper: QuestionPaper, user: User) -> list[dict]:
    """Question-bank grounding for generation: a fresh random sample of the
    paper's QA-dataset questions on its selected topics (or, with no topics
    picked, its chapters), split across datasets by their share percent.
    Random on purpose - regenerating the same blueprint sees a different
    slice of the bank, so papers don't come out identical. The kg service
    decides how much to reuse verbatim vs. write fresh (see its prompt).
    """
    topic_ids = (
        (await session.execute(select(QuestionPaperTopic.topic_id).where(QuestionPaperTopic.paper_id == paper.id)))
        .scalars()
        .all()
    )
    chapter_ids = (
        (await session.execute(select(QuestionPaperChapter.chapter_id).where(QuestionPaperChapter.paper_id == paper.id)))
        .scalars()
        .all()
    )
    if not topic_ids and not chapter_ids:
        return []
    in_scope = DatasetQuestion.topic_id.in_(topic_ids) if topic_ids else DatasetQuestion.chapter_id.in_(chapter_ids)

    shares = await _bank_shares(session, paper, user)
    total_percent = sum(p for _, p in shares) or 1

    seen: set[str] = set()
    references: list[dict] = []
    for dataset_id, percent in shares:
        limit = max(1, round(_REFERENCE_POOL_SIZE * percent / total_percent))
        rows = (
            await session.execute(
                select(DatasetQuestion)
                .where(DatasetQuestion.dataset_id == dataset_id, in_scope, text_only_filter())  # no figure to lean on
                .order_by(func.random())
                .limit(limit)
            )
        ).scalars().all()
        for q in rows:
            key = q.external_uid or str(q.id)
            if key in seen:  # same question sits in several datasets
                continue
            seen.add(key)
            references.append(
                {
                    "id": q.id,
                    "text": q.text,
                    "answer": q.answer,
                    "options": q.options,
                    "bloom_level": q.bloom_level.value,
                    "difficulty": q.difficulty.value if q.difficulty else None,
                    "question_type": q.question_type.value,
                }
            )
    return references


def _assign_marks(paper: QuestionPaper, sections: list[QuestionPaperSection], question_count: int) -> list[int]:
    """Real per-question marks, replacing the old hardcoded `marks=1`:
    follow the paper's own QuestionPaperSections (marks_each per slot) if
    it has any, else spread total_marks as evenly as the count allows.
    """
    marks_sequence = [s.marks_each for s in sections for _ in range(s.question_count)]
    if not marks_sequence:
        total_marks = paper.total_marks or question_count or 1
        n = max(question_count, 1)
        base, remainder = divmod(total_marks, n)
        marks_sequence = [base + (1 if i < remainder else 0) for i in range(n)]
    if len(marks_sequence) < question_count:
        pad_value = marks_sequence[-1] if marks_sequence else 1
        marks_sequence = marks_sequence + [pad_value] * (question_count - len(marks_sequence))
    return marks_sequence[:question_count]


_DEFAULT_BLOOM = {"REMEMBER": 20, "UNDERSTAND": 30, "APPLY": 30, "ANALYZE": 20}
_DEFAULT_DIFFICULTY = {"EASY": 30, "MEDIUM": 50, "HARD": 20}
_BLOOM_ORDER = [level.value for level in BloomLevel]
_DIFFICULTY_ORDER = ["EASY", "MEDIUM", "HARD"]


def _kg_question_types(question_types: list[str]) -> list[str]:
    """Paper question types (enum values or CBSE labels like "Short Answer II")
    -> the kg generator's four."""
    out: list[str] = []
    for t in question_types:
        u = t.upper()
        kind = ("mcq" if "MCQ" in u or "ASSERTION" in u or "TRUE" in u
                else "long_answer" if "LONG" in u
                else "numerical" if "NUMERICAL" in u
                else "short_answer")
        if kind not in out:
            out.append(kind)
    return out or ["mcq", "short_answer", "long_answer"]


def _by_marks(marks: list[int], percents: dict[str, int], order: list[str]) -> list[str]:
    """One level per slot so each level's share of MARKS tracks its percent:
    biggest slots first, each to the level furthest below its target."""
    total, scale = sum(marks), sum(percents.values())
    deficit = {level: percents.get(level, 0) * total / scale for level in order if percents.get(level, 0) > 0}
    out = [""] * len(marks)
    for i in sorted(range(len(marks)), key=lambda i: -marks[i]):
        level = max(deficit, key=lambda lv: deficit[lv])  # ties: earliest in order
        out[i] = level
        deficit[level] -= marks[i]
    return out


def _plan_slots(marks: list[int], labels: list[str | None], bloom: dict[str, int],
                difficulty: dict[str, int], question_types: list[str],
                slot_types: list[list[str] | None] | None = None) -> list[dict]:
    """The paper as an ordered list of question slots for the kg generator.
    `slot_types` (per slot, from its section) overrides the paper's types."""
    paper_types = _kg_question_types(question_types)
    levels = _by_marks(marks, bloom if sum(bloom.values()) else _DEFAULT_BLOOM, _BLOOM_ORDER)
    diffs = _by_marks(marks, difficulty if sum(difficulty.values()) else _DEFAULT_DIFFICULTY, _DIFFICULTY_ORDER)
    # T-070: the same levels, but a 4+ mark question is never EASY and a 1-mark one never HARD while a
    # smaller/bigger slot holds the level (swapped, so every level keeps its count)
    for i, m in enumerate(marks):
        bad, want = ("EASY", lambda j: marks[j] < 4 and diffs[j] != "EASY") if m >= 4 else (
            ("HARD", lambda j: marks[j] > 1 and diffs[j] != "HARD") if m == 1 else (None, None))
        if bad and diffs[i] == bad and (j := next((j for j in range(len(marks)) if want(j)), None)) is not None:
            diffs[i], diffs[j] = diffs[j], diffs[i]

    def kind(m: int, own: list[str] | None) -> str:
        types = _kg_question_types(own) if own else paper_types
        want = ["mcq", "short_answer"] if m == 1 else ["long_answer", "short_answer"] if m >= 4 else ["short_answer", "numerical"]
        return next((t for t in want if t in types), types[0])

    own_types = slot_types or [None] * len(marks)
    return [{"section": label, "marks": m, "bloom_level": b, "difficulty": d, "question_type": kind(m, own)}
            for m, label, b, d, own in zip(marks, labels, levels, diffs, own_types)]


async def _weigh_chapters(session: AsyncSession, paper: QuestionPaper, slots: list[dict]) -> None:
    """With the teacher's chapter weightage set, gives each slot a chapter so
    each chapter's share of the paper's marks tracks its marks (same rule as
    Bloom and difficulty). Without it, the generator spreads questions freely."""
    rows = (await session.execute(
        select(Chapter, QuestionPaperChapter.marks).join(QuestionPaperChapter, QuestionPaperChapter.chapter_id == Chapter.id)
        .where(QuestionPaperChapter.paper_id == paper.id, QuestionPaperChapter.marks > 0)
        .order_by(Chapter.order_index))).all()
    if not rows or not slots:
        return
    chapters = {str(c.id): c for c, _ in rows}
    picks = _by_marks([s["marks"] for s in slots], {str(c.id): m for c, m in rows}, list(chapters))
    for slot, chapter_id in zip(slots, picks):
        chapter = chapters[chapter_id]
        slot.update(chapter_id=chapter.id, chapter=chapter.name, chapter_number=chapter.order_index)


def _by_slot(generated: list[dict], count: int) -> dict[int, dict]:
    """kg questions keyed by the open slot they fill: by slot_index, or by
    position when kg sent none, so a missing or swapped question can't shift
    every later question's marks and section."""
    out: dict[int, dict] = {}
    for n, q in enumerate(generated):
        index = q.get("slot_index")
        index = n if index is None else index
        if 0 <= index < count:
            out.setdefault(index, q)
    return out


def _kept_slots(keep: list[QuestionPaperQuestion], slots: list[dict], sections: list[str]) -> dict:
    """Kept question id -> the blueprint slot it fills: the first free slot
    that matches it best (section, then marks, then Bloom level and
    difficulty), so fill-missing finds a gap mid-paper instead of assuming
    the kept questions are the first slots."""
    taken: dict = {}
    prev = -1
    for q in keep:
        def fit(i, q=q, prev=prev):
            slot, value = slots[i], lambda e: getattr(e, "value", e)
            return ((sections[i] if i < len(sections) else None) == q.section_name, slot["marks"] == q.marks,
                    slot["bloom_level"] == value(q.bloom_level), slot["difficulty"] == value(q.difficulty),
                    i > prev, -i)  # ties: the first slot after the previous kept question
        free = [i for i in range(len(slots)) if i not in taken.values()]
        taken[q.id] = prev = max(free, key=fit)
    return taken


def _mcq_ok(q: dict) -> bool:
    """Every paper question is saved here: an MCQ without 4 real options
    (C-009: AI MCQs printed with none) leaves its slot empty instead."""
    if (q.get("question_type") or "").upper() != "MCQ":
        return True
    options = q.get("options") or []
    # four real, different options (T-062: "(a) Greater with water" and "(d) Greater with water" in one question)
    return len(options) == 4 and all(str(o).strip() for o in options) and len({str(o).strip().lower() for o in options}) == 4


def _question_type(value: str | None) -> QuestionType | None:
    if not value:
        return None
    return _enum_or_none(QuestionType, "SHORT_ANSWER" if value.upper() == "NUMERICAL" else value.upper())


@router.post("/question-papers/{paper_id}/generate")
async def generate_paper(
    paper_id: str,
    body: GeneratePaperIn | None = None,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    return await _generate(session, paper, user, keep=[], diagrams=body.diagram_questions if body else None)


@router.post("/question-papers/{paper_id}/fill-missing", summary="Let Milo write the questions the paper is short of")
async def fill_missing(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    """Keeps every question the paper has and has Milo write the rest of the
    blueprint's slots (no bank this time - the bank already ran short)."""
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.status not in (QuestionPaperStatus.VALIDATION_FAILED, QuestionPaperStatus.REVIEW):
        raise ConflictError("Only a paper that needs a fix or is in review can be filled.")
    return await _generate(session, paper, user, keep=await load_questions(session, paper.id))


async def _generate(session: AsyncSession, paper: QuestionPaper, user: User, *,
                    keep: list[QuestionPaperQuestion], diagrams: int | None = None) -> QuestionPaperOut:
    """Fills the blueprint's slots: all of them (bank first, then Milo), or
    with `keep`, only the slots after the kept questions (Milo only)."""
    bloom_dist = await session.execute(
        select(QuestionPaperBloomDistribution).where(QuestionPaperBloomDistribution.paper_id == paper.id)
    )
    bloom_distribution = {d.bloom_level.value: d.value for d in bloom_dist.scalars().all()}

    difficulty_dist = await session.execute(
        select(QuestionPaperDifficultyDistribution).where(
            QuestionPaperDifficultyDistribution.paper_id == paper.id
        )
    )
    difficulty_distribution = {d.level.value: d.value for d in difficulty_dist.scalars().all()}

    sections_result = await session.execute(
        select(QuestionPaperSection).where(QuestionPaperSection.paper_id == paper.id)
    )
    sections = list(sections_result.scalars().all())
    target_count = sum(s.question_count for s in sections) if sections else (paper.total_marks or 10)

    chapter_refs, topic_names = await _resolve_kg_scope(session, paper)
    requested_chapter_numbers = {c["number"] for c in chapter_refs}

    plan_marks = _assign_marks(paper, sections, target_count)
    slots = _plan_slots(plan_marks, [s.label for s in sections for _ in range(s.question_count)] or [None] * target_count,
                        bloom_distribution, difficulty_distribution, paper.question_types or [],
                        [s.question_types for s in sections for _ in range(s.question_count)] or None)

    await _weigh_chapters(session, paper, slots)

    questions: list[dict] = []
    slot_of: list[int] = []
    references: list[dict] = []
    if keep and len(keep) >= len(slots):
        raise ConflictError("This paper already has every question its blueprint asks for.")
    section_sequence = [s.label for s in sections for _ in range(s.question_count)]
    kept_slot = _kept_slots(keep, slots, section_sequence)
    free = [i for i in range(len(slots)) if i not in kept_slot.values()]
    if chapter_refs:
        subject_name, board, grade = await _paper_kg_identity(session, paper)
        references = await _dataset_reference_questions(session, paper, user)
        from_bank = {} if keep else await _bank_slots(session, paper, user, slots, diagrams)
        from src.services.paper_parallel import _scope_chapter_ids  # paper_parallel imports this module

        filled = dict(from_bank)
        pending = [i for i in free if i not in from_bank]
        bank_ids = [d for d, _ in await _bank_shares(session, paper, user)]
        chapter_ids = await _scope_chapter_ids(session, paper)
        used = [k.text for k in keep]
        kept_items = [(k.text, k.options, k.answer) for k in keep]
        soft: dict[int, dict] = {}  # slot -> Milo's question that failed only the picky checks: the last resort
        kg_error: KgServiceUnavailableError | None = None
        started = time.monotonic()

        async def ask_milo(strict: bool) -> None:
            """One kg call for the open slots. A slot stays open, with its reason logged, when kg sent nothing
            for it or the question is unusable (T-028: every drop used to be silent)."""
            nonlocal kg_error
            try:
                generated = _by_slot(await kg_client.generate_paper_questions(
                    subject=subject_name, board=board, grade=grade, chapters=chapter_refs, topic_names=topic_names,
                    bloom_distribution=bloom_distribution, difficulty_distribution=difficulty_distribution,
                    question_types=paper.question_types or [], total_questions=len(pending),
                    reference_questions=[{k: v for k, v in r.items() if k != "id"} for r in references],
                    slots=[{k: v for k, v in slots[i].items() if k != "chapter_id"} for i in pending],
                ), len(pending))
            except KgServiceUnavailableError as exc:
                kg_error = exc
                logger.warning(f"paper_kg_failed paper={paper.id} open={len(pending)}")
                return
            on_paper = lambda: [*kept_items, *((f["text"], f.get("options"), f.get("answer")) for f in filled.values())]  # noqa: E731
            for n, i in enumerate(list(pending)):
                q = generated.get(n)
                reason = ("kg_sent_nothing" if not q else "bad_options" if not _mcq_ok(q)
                          else "repeat" if any(same_question((q["text"], q.get("options"), q.get("answer")), o) for o in on_paper())
                          else "crowded" if (concept_full(q["text"], [o[0] for o in on_paper()], len(slots), [g["text"] for g in generated.values()])  # T-070
                                             or topic_full(q.get("topic_id") or q.get("topic"), [f.get("topic_id") or f.get("topic") for f in filled.values()], len(slots)))
                          else "unfit" if unfit_for_paper(q, slots[i]["marks"]) else None)
                if reason == "crowded":
                    soft.setdefault(i, q)  # B28: only used as the last resort, flagged
                if reason == "unfit":
                    soft[i] = q
                    if not strict:
                        reason = None
                if reason:
                    logger.info(f"paper_slot_open paper={paper.id} slot={i} reason={reason} marks={slots[i]['marks']} "
                                f"type={slots[i]['question_type']} bloom={slots[i]['bloom_level']}")
                    continue
                filled[i] = q
            pending[:] = [i for i in pending if i not in filled]

        def on_paper_now() -> list:
            return [*kept_items, *((f["text"], f.get("options"), f.get("answer")) for f in filled.values())]

        async def backfill(relax: int) -> None:
            for i in list(pending):
                if bank_ids and chapter_ids:
                    await _pick_bank_slot(session, slots, i, filled, used + [f["text"] for f in filled.values()],
                                          bank_ids, chapter_ids, False, relax, others=kept_items)
            pending[:] = [i for i in pending if i not in filled]

        for _ in range(2):  # T-028: a paper short of a question asks again for just the missing ones
            if pending:
                await ask_milo(strict=True)
        # still short: the question bank covers what Milo could not, loosening one requirement at a time, then Milo
        # once more taking questions the picky checks refused, then the bank with nothing but "not an exact repeat"
        for relax in (0, 1, 2):
            await backfill(relax)
        if pending and time.monotonic() - started < 150:  # the request still has to answer in time
            await ask_milo(strict=False)
        await backfill(3)  # B28: marks/chapter loosened, the concept cap still kept
        for i in list(pending):  # Milo's picky-check misses, unless they would break the concept cap
            if i in soft and not concept_full(soft[i]["text"], [o[0] for o in on_paper_now()], len(slots)):
                filled[i] = soft[i]
        pending[:] = [i for i in pending if i not in filled]
        if pending and time.monotonic() - started < 150:  # ask kg again for what is still open
            await ask_milo(strict=False)
        if pending:  # last resort: over the cap beats a short paper, and the paper says so
            await backfill(4)
            for i in list(pending):
                if i in soft:
                    filled[i] = soft[i]
            pending[:] = [i for i in pending if i not in filled]
        if pending:
            logger.error(f"paper_short paper={paper.id} open={len(pending)} of={len(slots)}")
            if kg_error and not filled:
                raise kg_error
        picked = [(i, filled[i]) for i in free if i in filled and _mcq_ok(filled[i])]
        questions = [q for _, q in picked]
        slot_of = [i for i, _ in picked]

    if not keep:
        existing = await session.execute(
            select(QuestionPaperQuestion).where(QuestionPaperQuestion.paper_id == paper.id)
        )
        for row in existing.scalars().all():
            await session.delete(row)
        await session.flush()

    chapters_by_number = {c.order_index: c for c in (await session.execute(
        select(Chapter).where(Chapter.subject_id == paper.subject_id, Chapter.order_index.is_not(None))
    )).scalars().all()}
    topics = (await session.execute(
        select(Topic).where(Topic.chapter_id.in_([c.id for c in chapters_by_number.values()]))
    )).scalars().all() if chapters_by_number else []
    bank_by_text = {normalize(r["text"]): r["id"] for r in references}
    placed = [(kept_slot[k.id], k) for k in keep]
    for q, i in zip(questions, slot_of):
        chapter = (next((c for c in chapters_by_number.values() if c.id == q["chapter_id"]), None)
                   if q.get("chapter_id") else chapters_by_number.get(q.get("chapter_number")))
        topic = (next((t for t in topics if t.id == q["topic_id"]), None) if q.get("topic_id") else
                 next((t for t in topics if chapter and t.chapter_id == chapter.id and t.name == q.get("topic")), None))
        # the kg prompt allows "minimal edits" to a reused bank question, so match closely, not exactly
        close = difflib.get_close_matches(normalize(q["text"]), list(bank_by_text), n=1, cutoff=0.8)
        bank_id = q.get("bank_id") or (bank_by_text[close[0]] if close else None)
        placed.append((i, QuestionPaperQuestion(
            paper_id=paper.id, order=0, marks=slots[i]["marks"], text=q["text"],
            # T-070: a 1-mark question that only asks for a fact is not Evaluate/Create
            bloom_level=("UNDERSTAND" if over_marked(q["text"], slots[i]["marks"], q["bloom_level"])  # T-070: 3+ marks is not recall
                         else "REMEMBER" if q["bloom_level"] in ("EVALUATE", "CREATE") and plain_recall(q["text"], slots[i]["marks"])
                         else q["bloom_level"]),
            question_type=_question_type(q.get("question_type")) or _question_type(slots[i]["question_type"]),
            difficulty=_enum_or_none(PaperDifficultyLevel, q.get("difficulty")),
            chapter_id=chapter.id if chapter else None, topic_id=topic.id if topic else None,
            section_name=section_sequence[i] if i < len(section_sequence) else None,
            options=q.get("options"), answer=q.get("answer"),
            source=QuestionSource.BANK.value if bank_id else QuestionSource.AI.value, dataset_question_id=bank_id,
        )))
    # T-070: a 8+ question paper planned with Hard questions must not come back with none: the heaviest higher-order
    # new questions in Hard slots take the label. ponytail: marks/Bloom heuristic, not a re-grade.
    fresh = [(i, r) for i, r in placed if r not in keep]
    if len(placed) >= 8 and not any(r.difficulty == PaperDifficultyLevel.HARD for _, r in placed):
        hard_slots = [(i, r) for i, r in fresh if str(getattr(slots[i]["difficulty"], "value", slots[i]["difficulty"])) == "HARD"
                      and str(getattr(r.bloom_level, "value", r.bloom_level)) in ("APPLY", "ANALYZE", "EVALUATE", "CREATE")]
        for _, r in sorted(hard_slots, key=lambda p: -p[1].marks):
            r.difficulty = PaperDifficultyLevel.HARD
        if not any(r.difficulty == PaperDifficultyLevel.HARD for _, r in placed):  # B28: none planned (e.g. a 20-mark test)
            # the heaviest question that asks for more than recall; ponytail: relabel, not a request to kg
            rank = lambda r: (str(getattr(r.bloom_level, "value", r.bloom_level)) != "REMEMBER", r.marks > 1, r.marks)  # noqa: E731
            if (pool := fresh or placed):
                max(pool, key=lambda p: rank(p[1]))[1].difficulty = PaperDifficultyLevel.HARD
    # paper order follows the blueprint's slots, so a gap mid-paper is filled where it is
    for order, (_, row) in enumerate(sorted(placed, key=lambda p: p[0]), start=1):
        row.order = order
        session.add(row)

    existing_validation = await session.execute(
        select(QuestionPaperValidation).where(QuestionPaperValidation.paper_id == paper.id)
    )
    validation = existing_validation.scalar_one_or_none()

    number_of = {c.id: n for n, c in chapters_by_number.items()}
    covered_chapters = {q["chapter_number"] for q in questions} | {number_of.get(k.chapter_id) for k in keep}
    coverage_percent = (
        round(100 * len(covered_chapters & requested_chapter_numbers) / len(requested_chapter_numbers))
        if requested_chapter_numbers
        else (100 if questions else 0)
    )
    duplicate_count = len(duplicates_of([(k.text, k.options, k.answer) for k in keep]
                                        + [(q["text"], q.get("options"), q.get("answer")) for q in questions]))
    total = len(keep) + len(questions)
    quality_percent = max(0, 100 - duplicate_count * 20) if total else 0
    quality_percent = min(quality_percent, coverage_percent) if total else 0
    marks_total = sum(row.marks for _, row in placed)  # what the questions really add up to (T-028)
    if slots and total:
        quality_percent = min(quality_percent, round(100 * total / len(slots)))

    issues: list[str] = []
    if total < len(slots) and total:
        issues.append(f"{len(slots) - total} of the blueprint's {len(slots)} questions couldn't be filled.")
    if not total:
        issues.append(
            "No questions could be generated for this configuration."
            if chapter_refs
            else "No chapters/topics with knowledge-graph content are selected on this paper."
        )
    if paper.total_marks and total and marks_total != paper.total_marks:
        issues.append(f"The questions add up to {marks_total} of the paper's {paper.total_marks} marks.")
    wanted = diagram_quota(len(slots), diagrams) if not keep else 0
    if wanted and not await figure_svgs(session, [q["bank_id"] for q in questions if q.get("bank_id")]):
        issues.append("No question with a figure is in this school's question banks for these chapters. "
                      "Ask your admin to add a bank that has diagrams.")  # C-020: say so instead of silently skipping
    for n, word in crowded([k.text for k in keep] + [q["text"] for q in questions], len(slots)).items():
        issues.append(f'A 3rd question about "{word}" was kept because the paper could not be filled otherwise.')  # B28
    if duplicate_count:
        issues.append(f"{duplicate_count} duplicate question(s) were generated.")
    if requested_chapter_numbers and coverage_percent < 100:
        issues.append("Not every selected chapter/topic produced questions.")

    if validation is None:
        validation = QuestionPaperValidation(paper_id=paper.id)
        session.add(validation)
    validation.coverage_percent = coverage_percent
    validation.marks_accounted_for = marks_total
    validation.duplicate_count = duplicate_count
    validation.quality_percent = quality_percent
    validation.issues = issues

    paper.status = QuestionPaperStatus.REVIEW if total else QuestionPaperStatus.VALIDATION_FAILED
    if questions:
        await create_version(session, paper, created_by=user.id,
                             summary="Milo wrote the missing questions" if keep else "Generated paper", origin="generate")
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)


def _enum_or_none(enum, value):
    try:
        return enum(value) if value else None
    except ValueError:
        return None


async def _paper_questions_out(session: AsyncSession, paper_id: str) -> ListEnvelope[QuestionPaperQuestionOut]:
    questions = await load_questions(session, paper_id)
    svgs = await figure_svgs(session, [q.dataset_question_id for q in questions], captions=True)
    items = [question_out(q).model_copy(update=svgs.get(q.dataset_question_id, {})) for q in questions]
    return ListEnvelope(items=items, total=len(items))


@router.get("/question-papers/{paper_id}/questions")
async def list_paper_questions(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperQuestionOut]:
    await _get_paper_to_edit(session, paper_id, user)
    return await _paper_questions_out(session, paper_id)


@router.patch("/question-papers/{paper_id}/questions/{question_id}")
async def update_paper_question(
    paper_id: str,
    question_id: str,
    body: UpdateQuestionPaperQuestionIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperQuestionOut]:
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.status != QuestionPaperStatus.REVIEW:
        raise ConflictError("Paper isn't in REVIEW.")
    result = await session.execute(
        select(QuestionPaperQuestion).options(undefer_group(COPILOT_COLUMNS)).where(
            QuestionPaperQuestion.id == question_id, QuestionPaperQuestion.paper_id == paper_id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        raise NotFoundError(f'No question "{question_id}" on this paper.')
    if body.dataset_question_id:  # a bank question (e.g. one with a figure) swapped in whole
        bank = (await session.execute(select(DatasetQuestion).where(
            DatasetQuestion.id == body.dataset_question_id,
            DatasetQuestion.dataset_id.in_([d for d, _ in await _bank_shares(session, paper, user)]),
            text_only_filter(pictures=True)))).scalar_one_or_none()
        if bank is None:
            raise NotFoundError("That question isn't in this paper's question bank.")
        question.text, question.options, question.answer = bank.text, bank.options, bank.answer
        question.question_type = bank.question_type or question.question_type
        question.dataset_question_id, question.source = bank.id, QuestionSource.BANK.value
        question.chapter_id, question.topic_id = bank.chapter_id or question.chapter_id, bank.topic_id
    else:
        question.text = body.text
        question.source = QuestionSource.EDITED.value
        if body.replace:  # a different question: the old bank question's figure is no longer its figure
            question.dataset_question_id = None
    await create_version(session, paper, created_by=user.id, summary=f"Edited question {question.order}",
                         origin="manual")
    await session.commit()
    return await _paper_questions_out(session, paper_id)


def is_choice_question(question) -> bool:
    return bool(question.options) or str(getattr(question.question_type, "value", question.question_type)) == "MCQ"


@router.get("/question-papers/{paper_id}/questions/{question_id}/candidates", summary="Shuffle picker")
async def get_question_candidates(
    paper_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperQuestionCandidateOut]:
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.status != QuestionPaperStatus.REVIEW:
        raise ConflictError("Paper isn't in REVIEW.")
    on_paper = await load_questions(session, paper_id)
    question = next((q for q in on_paper if str(q.id) == question_id), None)
    if question is None:
        raise NotFoundError(f'No question "{question_id}" on this paper.')

    chapter_refs, topic_names = await _resolve_kg_scope(session, paper)
    candidates: list[dict] = []
    # C-017: an AI alternative is text only; on an MCQ it would sit above the old choices and key.
    # An MCQ is shuffled only with whole bank questions (_diagram_candidates), which bring their own options.
    if chapter_refs and not is_choice_question(question):
        subject_name, board, grade = await _paper_kg_identity(session, paper)
        candidates = await kg_client.generate_replacement_candidates(
            subject=subject_name,
            board=board,
            grade=grade,
            chapters=chapter_refs,
            topic_names=topic_names,
            bloom_level=question.bloom_level.value,
            exclude_text=question.text,
            count=3,
        )
    items = [
        QuestionPaperQuestionCandidateOut(id=f"{question_id}-alt-{i + 1}", text=c["text"])
        for i, c in enumerate(candidates)
        if not unfit_for_paper(c, question.marks)
    ]
    items += await _diagram_candidates(session, paper, user, question, [q.text for q in on_paper])
    return ListEnvelope(items=items, total=len(items))


async def _diagram_candidates(session: AsyncSession, paper: QuestionPaper, user: User,
                              question: QuestionPaperQuestion, taken: list[str]) -> list[QuestionPaperQuestionCandidateOut]:
    """Up to 2 question-bank questions with a figure that fit this question's slot - a swap can bring a picture."""
    from src.services.paper_parallel import _bank_pick, _scope_chapter_ids

    dataset_ids = [d for d, _ in await _bank_shares(session, paper, user)]
    chapter_ids = await _scope_chapter_ids(session, paper)
    if not dataset_ids or not chapter_ids:
        return []
    banks = []
    for relax in (0, 1, 2):  # C-020: no figure question of exactly this slot - offer the nearest ones
        while len(banks) < 2 and (bank := await _bank_pick(session, question, dataset_ids=dataset_ids,
                                                           chapter_ids=chapter_ids, taken=taken, figures=True,
                                                           relax=relax)) is not None:
            banks.append(bank)
            taken = [*taken, bank.text]
    figures = await figure_svgs(session, [b.id for b in banks], captions=True)
    return [QuestionPaperQuestionCandidateOut(id=f"{question.id}-bank-{b.id}", text=b.text, dataset_question_id=str(b.id),
                                              options=b.options, **figures.get(b.id, {})) for b in banks]


@router.put("/question-papers/{paper_id}/questions/order")
async def reorder_questions(
    paper_id: str,
    body: ReorderQuestionsIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperQuestionOut]:
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.status != QuestionPaperStatus.REVIEW:
        raise ConflictError("Paper isn't in REVIEW.")
    result = await session.execute(
        select(QuestionPaperQuestion).where(QuestionPaperQuestion.paper_id == paper_id)
    )
    questions = {str(q.id): q for q in result.scalars().all()}
    if set(body.ordered_question_ids) != set(questions.keys()):
        raise ValidationError("orderedQuestionIds doesn't match the paper's current question set exactly.")

    for order, question_id in enumerate(body.ordered_question_ids, start=1):
        questions[question_id].order = order
    await session.commit()
    return await _paper_questions_out(session, paper_id)


@router.post("/question-papers/{paper_id}/finalize")
async def finalize_paper(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_to_edit(session, paper_id, user)
    if paper.status != QuestionPaperStatus.REVIEW:
        raise ConflictError("Only a paper in review can be finalized.")
    marks = (await session.execute(select(func.coalesce(func.sum(QuestionPaperQuestion.marks), 0))
                                   .where(QuestionPaperQuestion.paper_id == paper.id))).scalar_one()
    if paper.total_marks and marks != paper.total_marks:  # T-028: the header must be true
        raise ConflictError(f"The questions add up to {marks} of the paper's {paper.total_marks} marks - "
                            "let Milo write the missing ones, or change the blueprint, before finalizing.")
    paper.status = QuestionPaperStatus.FINALIZED
    # T-028: freeze the instructions as rendered, so they match the sections that were finalized
    paper.instructions = (await _serialize(session, paper)).instructions
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)
