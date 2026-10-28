from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError

from src.api.deps import get_db_session, require_role
from src.api.schemas.bloom import BloomScore
from src.api.schemas.common import ListEnvelope
from src.api.schemas.test_result import (
    AnswerHighlights,
    AnswerQuote,
    ClassTestResultOut,
    StudentAnswerOut,
    StudentTestResultDetailOut,
    StudentTestResultOut,
)
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    BloomLevel,
    Chapter,
    Role,
    StudentTestResult,
    StudentTestResultBloomScore,
    Topic,
    User,
    VoiceTest,
    VoiceTestQuestion,
)
from src.repositories.lookups import get_test_for_teacher
from src.services.evaluation_service import _normalize_bloom_label

router = APIRouter(prefix="/api/v1", tags=["test-results"])


async def _bloom_performance_for(session: AsyncSession, result_ids: list) -> list[BloomScore]:
    if not result_ids:
        return [BloomScore(level=level, percent=None) for level in BloomLevel]
    rows = await session.execute(
        select(StudentTestResultBloomScore.bloom_level, func.avg(StudentTestResultBloomScore.percent))
        .where(StudentTestResultBloomScore.student_test_result_id.in_(result_ids))
        .group_by(StudentTestResultBloomScore.bloom_level)
    )
    averages = {level: pct for level, pct in rows.all() if pct is not None}
    return [
        BloomScore(level=level, percent=round(averages[level]) if level in averages else None)
        for level in BloomLevel
    ]


async def _topic_label(session: AsyncSession, test: VoiceTest) -> str:
    if test.topic_id is not None:
        result = await session.execute(select(Topic.name).where(Topic.id == test.topic_id))
        name = result.scalar_one_or_none()
        if name:
            return name
    result = await session.execute(select(Chapter.name).where(Chapter.id == test.chapter_id))
    return result.scalar_one_or_none() or ""


@router.get("/tests/{test_id}/results/class")
async def get_class_result(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ClassTestResultOut:
    test = await get_test_for_teacher(session, test_id, user, ready=True)

    results = await session.execute(
        select(StudentTestResult).where(StudentTestResult.test_id == test.id)
    )
    student_results = results.scalars().all()
    result_ids = [r.id for r in student_results]

    class_mastery_percent = (
        round(sum(r.mastery_percent for r in student_results) / len(student_results))
        if student_results
        else 0
    )
    bloom_performance = await _bloom_performance_for(session, result_ids)
    assessed = [b.percent for b in bloom_performance if b.percent is not None]
    priority_gap_percent = min(assessed) if assessed else class_mastery_percent

    return ClassTestResultOut(
        test_id=str(test.id),
        class_mastery_percent=class_mastery_percent,
        assigned_count=test.assigned_count,
        completed_count=test.completed_count,
        priority_gap_topic=await _topic_label(session, test),
        priority_gap_percent=priority_gap_percent,
        bloom_performance=bloom_performance,
    )


@router.get("/tests/{test_id}/results/students")
async def list_student_results(
    test_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[StudentTestResultOut]:
    test = await get_test_for_teacher(session, test_id, user, ready=True)
    results = await session.execute(
        select(StudentTestResult).where(StudentTestResult.test_id == test.id)
    )
    student_results = results.scalars().all()
    items = [
        StudentTestResultOut(
            test_id=str(test.id),
            student_id=str(r.student_id),
            mastery_percent=r.mastery_percent,
            bloom_performance=await _bloom_performance_for(session, [r.id]),
        )
        for r in student_results
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/tests/{test_id}/results/students/{student_id}")
async def get_student_result(
    test_id: str,
    student_id: str,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> StudentTestResultDetailOut:
    test = await get_test_for_teacher(session, test_id, user, ready=True)
    result = await session.execute(
        select(StudentTestResult).where(
            StudentTestResult.test_id == test.id, StudentTestResult.student_id == student_id
        )
    )
    student_result = result.scalar_one_or_none()
    if student_result is None:
        raise NotFoundError("Student didn't attempt this Test, or isn't in your class.")
    answers = await _answers(session, test, student_result.student_id)
    scored = [a for a in answers if a.score is not None and a.quote]
    best = max(scored, key=lambda a: a.score, default=None)
    weakest = min(scored, key=lambda a: a.score, default=None)
    return StudentTestResultDetailOut(
        test_id=str(test.id),
        student_id=student_id,
        mastery_percent=student_result.mastery_percent,
        bloom_performance=await _bloom_performance_for(session, [student_result.id]),
        answers=answers,
        highlights=AnswerHighlights(
            best=AnswerQuote(question_number=best.question_number, quote=best.quote) if best else None,
            weakest=(AnswerQuote(question_number=weakest.question_number, quote=weakest.quote)
                     if weakest and weakest.score < best.score else None),
        ),
    )


MAX_QUOTE = 160


async def _answers(session: AsyncSession, test: VoiceTest, student_id) -> list[StudentAnswerOut]:
    """Per question, from the student's latest graded session record:
    colearner's finish answers (version, nudges, first answer) joined with
    the grader's qa_transcripts (score) by question number."""
    records = (await session.execute(
        select(AiAssessorSessionRecord)
        .join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.test_id == test.id, AiAssessorSession.student_id == student_id,
               AiAssessorSessionRecord.needs_review.is_(False))
        .order_by(AiAssessorSessionRecord.created_at.desc())
    )).scalars().all()
    record = next((r for r in records if (r.report or {}).get("qa_transcripts")), None)
    if record is None:
        return []
    questions = {q.order: q for q in (await session.execute(
        select(VoiceTestQuestion).where(VoiceTestQuestion.voice_test_id == test.id))).scalars()}
    asked = {a.get("question_number"): a for a in record.answers or [] if isinstance(a, dict)}
    graded = {g.get("question_number"): g for g in record.report["qa_transcripts"] if isinstance(g, dict)}
    out = []
    for number in sorted(n for n in asked.keys() | graded.keys() if isinstance(n, int)):
        a, g, q = asked.get(number, {}), graded.get(number, {}), questions.get(number)
        version = "easier" if a.get("version") == "easier" else "standard"
        if q is None:
            text = str(g.get("question_asked") or "")
        else:
            text = q.easier_version["spoken_text"] if version == "easier" and q.easier_version else q.spoken_text
        quote = " ".join(str(a.get("first_answer") or g.get("student_response") or "").split())
        score = g.get("score_percent")
        out.append(StudentAnswerOut(
            question_number=number,
            bloom_level=q.bloom_level if q else _normalize_bloom_label(g.get("blooms_attribute")),
            question_text=text,
            version=version,
            nudges_used=int(a.get("hints_used") or g.get("hints_used") or 0),
            score=round(score) if isinstance(score, (int, float)) else None,
            quote=quote if len(quote) <= MAX_QUOTE else quote[:MAX_QUOTE - 1].rstrip() + "…",
            misconception=str(g.get("misconception") or "").strip() or None,
        ))
    return out
