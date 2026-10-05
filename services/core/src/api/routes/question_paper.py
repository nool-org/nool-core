from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.bloom import BloomTarget
from src.api.schemas.common import ListEnvelope
from src.api.schemas.dataset import DatasetShare
from src.api.schemas.question_paper import (
    CreateQuestionPaperIn,
    PaperDifficultyTarget,
    PaperRules,
    PaperSection,
    PaperValidationOut,
    QuestionPaperOut,
    QuestionPaperQuestionCandidateOut,
    QuestionPaperQuestionOut,
    ReorderQuestionsIn,
    UpdateQuestionPaperQuestionIn,
)
from src.domain.models import (
    Chapter,
    Dataset,
    DatasetQuestion,
    DatasetType,
    Feature,
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
    Role,
    School,
    SchoolClass,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
)
from src.repositories.dataset_access import ensure_datasets_visible
from src.services.subject_grades import ensure_subjects_enabled_for_school, ensure_subjects_fit_grade
from src.repositories.subscription_repository import get_active_plan
from src.repositories.usage_repository import count_question_papers
from src.services import kg_client
from src.services.bloom_defaults import resolve_bloom_targets

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
    chapters = await session.execute(
        select(QuestionPaperChapter.chapter_id).where(QuestionPaperChapter.paper_id == paper.id)
    )
    topics = await session.execute(
        select(QuestionPaperTopic.topic_id).where(QuestionPaperTopic.paper_id == paper.id)
    )
    sections = await session.execute(
        select(QuestionPaperSection).where(QuestionPaperSection.paper_id == paper.id)
    )
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

    return QuestionPaperOut(
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
        chapter_ids=[str(row[0]) for row in chapters.all()],
        topic_ids=[str(row[0]) for row in topics.all()],
        total_marks=paper.total_marks,
        duration_minutes=paper.duration_minutes,
        sections=[
            PaperSection(label=s.label, question_count=s.question_count, marks_each=s.marks_each)
            for s in sections.scalars().all()
        ],
        bloom_distribution=[
            BloomTarget(level=d.bloom_level, value=d.value) for d in bloom_dist.scalars().all()
        ],
        difficulty_distribution=[
            PaperDifficultyTarget(level=d.level, value=d.value) for d in difficulty_dist.scalars().all()
        ],
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
        instructions=paper.instructions,
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
    for chapter_id in body.chapter_ids or []:
        session.add(QuestionPaperChapter(paper_id=paper.id, chapter_id=chapter_id))
    for topic_id in body.topic_ids or []:
        session.add(QuestionPaperTopic(paper_id=paper.id, topic_id=topic_id))
    for section in body.sections or []:
        session.add(
            QuestionPaperSection(
                paper_id=paper.id,
                label=section.label,
                question_count=section.question_count,
                marks_each=section.marks_each,
            )
        )
    school = await session.get(School, paper.school_id)
    for target in resolve_bloom_targets(school, body.bloom_distribution):
        session.add(
            QuestionPaperBloomDistribution(paper_id=paper.id, bloom_level=target.level, value=target.value)
        )
    for target in body.difficulty_distribution or []:
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
    """A paper in the teacher's school that they also teach (subject + grade)
    - reading stays school-wide, changing it doesn't."""
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


@router.get("/question-papers")
async def list_papers(
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperOut]:
    result = await session.execute(select(QuestionPaper).where(QuestionPaper.school_id == user.school_id))
    items = [await _serialize(session, p) for p in result.scalars().all()]
    return ListEnvelope(items=items, total=len(items))


@router.get("/question-papers/{paper_id}")
async def get_paper(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_in_school(session, paper_id, user.school_id)
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


async def _dataset_reference_questions(session: AsyncSession, paper: QuestionPaper) -> list[dict]:
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

    shares = (
        await session.execute(
            select(QuestionPaperDatasetShare.dataset_id, QuestionPaperDatasetShare.percent)
            .join(Dataset, Dataset.id == QuestionPaperDatasetShare.dataset_id)
            .where(QuestionPaperDatasetShare.paper_id == paper.id, Dataset.type == DatasetType.QA)
        )
    ).all()
    total_percent = sum(p for _, p in shares) or 1

    seen: set[str] = set()
    references: list[dict] = []
    for dataset_id, percent in shares:
        limit = max(1, round(_REFERENCE_POOL_SIZE * percent / total_percent))
        rows = (
            await session.execute(
                select(DatasetQuestion)
                .where(DatasetQuestion.dataset_id == dataset_id, in_scope)
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


@router.post("/question-papers/{paper_id}/generate")
async def generate_paper(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> QuestionPaperOut:
    paper = await _get_paper_to_edit(session, paper_id, user)

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

    questions: list[dict] = []
    if chapter_refs:
        subject_name, board, grade = await _paper_kg_identity(session, paper)
        questions = await kg_client.generate_paper_questions(
            subject=subject_name,
            board=board,
            grade=grade,
            chapters=chapter_refs,
            topic_names=topic_names,
            bloom_distribution=bloom_distribution,
            difficulty_distribution=difficulty_distribution,
            question_types=paper.question_types or [],
            total_questions=target_count,
            reference_questions=await _dataset_reference_questions(session, paper),
        )

    existing = await session.execute(
        select(QuestionPaperQuestion).where(QuestionPaperQuestion.paper_id == paper.id)
    )
    for row in existing.scalars().all():
        await session.delete(row)
    await session.flush()

    marks_sequence = _assign_marks(paper, sections, len(questions))
    for order, (q, marks) in enumerate(zip(questions, marks_sequence), start=1):
        session.add(
            QuestionPaperQuestion(
                paper_id=paper.id, order=order, bloom_level=q["bloom_level"], marks=marks, text=q["text"]
            )
        )

    existing_validation = await session.execute(
        select(QuestionPaperValidation).where(QuestionPaperValidation.paper_id == paper.id)
    )
    validation = existing_validation.scalar_one_or_none()

    covered_chapters = {q["chapter_number"] for q in questions}
    coverage_percent = (
        round(100 * len(covered_chapters & requested_chapter_numbers) / len(requested_chapter_numbers))
        if requested_chapter_numbers
        else (100 if questions else 0)
    )
    texts = [q["text"].strip().lower() for q in questions]
    duplicate_count = len(texts) - len(set(texts))
    quality_percent = max(0, 100 - duplicate_count * 20) if questions else 0
    quality_percent = min(quality_percent, coverage_percent) if questions else 0

    issues: list[str] = []
    if not questions:
        issues.append(
            "No questions could be generated for this configuration."
            if chapter_refs
            else "No chapters/topics with knowledge-graph content are selected on this paper."
        )
    if duplicate_count:
        issues.append(f"{duplicate_count} duplicate question(s) were generated.")
    if requested_chapter_numbers and coverage_percent < 100:
        issues.append("Not every selected chapter/topic produced questions.")

    if validation is None:
        validation = QuestionPaperValidation(paper_id=paper.id)
        session.add(validation)
    validation.coverage_percent = coverage_percent
    validation.marks_accounted_for = sum(marks_sequence[: len(questions)])
    validation.duplicate_count = duplicate_count
    validation.quality_percent = quality_percent
    validation.issues = issues

    paper.status = QuestionPaperStatus.REVIEW if questions else QuestionPaperStatus.VALIDATION_FAILED
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)


async def _paper_questions_out(session: AsyncSession, paper_id: str) -> ListEnvelope[QuestionPaperQuestionOut]:
    result = await session.execute(
        select(QuestionPaperQuestion)
        .where(QuestionPaperQuestion.paper_id == paper_id)
        .order_by(QuestionPaperQuestion.order)
    )
    items = [
        QuestionPaperQuestionOut(
            id=str(q.id), paper_id=paper_id, order=q.order, bloom_level=q.bloom_level, marks=q.marks, text=q.text
        )
        for q in result.scalars().all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/question-papers/{paper_id}/questions")
async def list_paper_questions(
    paper_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperQuestionOut]:
    await _get_paper_in_school(session, paper_id, user.school_id)
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
        select(QuestionPaperQuestion).where(
            QuestionPaperQuestion.id == question_id, QuestionPaperQuestion.paper_id == paper_id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        raise NotFoundError(f'No question "{question_id}" on this paper.')
    question.text = body.text
    await session.commit()
    return await _paper_questions_out(session, paper_id)


@router.get("/question-papers/{paper_id}/questions/{question_id}/candidates", summary="Shuffle picker")
async def get_question_candidates(
    paper_id: str,
    question_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.QUESTION_PAPER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[QuestionPaperQuestionCandidateOut]:
    paper = await _get_paper_in_school(session, paper_id, user.school_id)
    if paper.status != QuestionPaperStatus.REVIEW:
        raise ConflictError("Paper isn't in REVIEW.")
    result = await session.execute(
        select(QuestionPaperQuestion).where(
            QuestionPaperQuestion.id == question_id, QuestionPaperQuestion.paper_id == paper_id
        )
    )
    question = result.scalar_one_or_none()
    if question is None:
        raise NotFoundError(f'No question "{question_id}" on this paper.')

    chapter_refs, topic_names = await _resolve_kg_scope(session, paper)
    candidates: list[dict] = []
    if chapter_refs:
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
    ]
    return ListEnvelope(items=items, total=len(items))


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
    paper.status = QuestionPaperStatus.FINALIZED
    await session.commit()
    await session.refresh(paper)
    return await _serialize(session, paper)
