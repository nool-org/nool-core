"""GET /me/homework/{id}/context's real, LLM-grounded insight generation
(see student_homework.py's _ensure_insight_generated and
src/services/colearner_insight_client.py) - generated once and cached on
the Homework row, falling back to the original generic placeholder if
colearner is unreachable. Real rows against the dev Postgres via
db_session, same seed pattern as test_homework_confirm_completion.py.
colearner_insight_client.generate_homework_insight is monkeypatched so
these tests never make a real network/LLM call.
"""

import uuid

from src.api.routes import student_homework
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Homework,
    HomeworkDifficulty,
    HomeworkStatus,
    Role,
    School,
    SchoolClass,
    StudentHomeworkProgress,
    StudentTestResult,
    StudentTestResultBloomScore,
    Subject,
    User,
    UserStatus,
    VoiceTest,
)
from src.services.colearner_insight_client import ColearnerInsightUnavailableError

_FAKE_INSIGHT = {
    "what_needs_understanding": "You need to work on applying the right-hand rule.",
    "references": [{"title": "Right-Hand Thumb Rule", "subtitle": "Direction of the field"}],
    "key_idea_title": "Magnetic field direction",
    "key_idea_body": "Point your thumb along the current; your fingers curl the field's direction.",
    "connection_prompt": "Think of how a compass needle reacts near a live wire.",
}


async def _seed_homework_with_student(db_session, *, textbook_context: str | None = "Chapter excerpt.") -> tuple[Homework, User]:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school, subject])
    await db_session.flush()
    school_class = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Magnetic Effects of Electric Current")
    db_session.add_all([school_class, chapter])
    await db_session.flush()

    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS,
        textbook_context=textbook_context,
    )
    db_session.add(test)
    await db_session.flush()

    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Student", role=Role.STUDENT, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()

    hw = Homework(
        test_id=test.id, class_id=school_class.id, gap_topic=chapter.name, gap_mastery_percent=52,
        total_questions=5, difficulty=HomeworkDifficulty.MIXED, target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS,
        status=HomeworkStatus.ASSIGNED, assigned_count=1,
    )
    db_session.add(hw)
    await db_session.flush()
    db_session.add(StudentHomeworkProgress(homework_id=hw.id, student_id=student.id))
    await db_session.flush()

    return hw, student


async def _seed_bloom_scores(db_session, hw: Homework, student: User, scores: dict[BloomLevel, int | None]) -> None:
    result = StudentTestResult(test_id=hw.test_id, student_id=student.id, mastery_percent=52)
    db_session.add(result)
    await db_session.flush()
    for level, percent in scores.items():
        db_session.add(
            StudentTestResultBloomScore(student_test_result_id=result.id, bloom_level=level, percent=percent)
        )
    await db_session.flush()


async def test_generates_and_persists_a_real_insight_on_first_call(db_session, monkeypatch):
    hw, student = await _seed_homework_with_student(db_session)
    await _seed_bloom_scores(
        db_session, hw, student,
        {BloomLevel.REMEMBER: 80, BloomLevel.UNDERSTAND: 70, BloomLevel.ANALYZE: 30},
    )

    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _FAKE_INSIGHT

    monkeypatch.setattr(student_homework, "generate_homework_insight", fake_generate)

    context = await student_homework.get_homework_context(
        homework_id=str(hw.id), user=student, session=db_session
    )

    assert context.what_needs_understanding == _FAKE_INSIGHT["what_needs_understanding"]
    assert context.key_idea_body == _FAKE_INSIGHT["key_idea_body"]
    assert [r.title for r in context.references] == ["Right-Hand Thumb Rule"]

    # The weakest *assessed* level (ANALYZE, 30%) was picked, not just the first one.
    assert captured["weak_bloom_level"] == "ANALYZE"
    assert captured["topic"] == hw.gap_topic
    assert captured["mastery_percent"] == hw.gap_mastery_percent

    await db_session.refresh(hw)
    assert hw.insight_generated_at is not None
    assert hw.insight_key_idea_title == _FAKE_INSIGHT["key_idea_title"]


async def test_second_call_is_served_from_cache_without_calling_the_generator_again(db_session, monkeypatch):
    hw, student = await _seed_homework_with_student(db_session)

    call_count = 0

    async def fake_generate(**kwargs):
        nonlocal call_count
        call_count += 1
        return _FAKE_INSIGHT

    monkeypatch.setattr(student_homework, "generate_homework_insight", fake_generate)

    first = await student_homework.get_homework_context(homework_id=str(hw.id), user=student, session=db_session)
    second = await student_homework.get_homework_context(homework_id=str(hw.id), user=student, session=db_session)

    assert call_count == 1
    assert second.key_idea_title == first.key_idea_title == _FAKE_INSIGHT["key_idea_title"]


async def test_falls_back_to_the_generic_placeholder_when_colearner_is_unreachable(db_session, monkeypatch):
    hw, student = await _seed_homework_with_student(db_session)

    async def failing_generate(**kwargs):
        raise ColearnerInsightUnavailableError("Could not reach the insight service.")

    monkeypatch.setattr(student_homework, "generate_homework_insight", failing_generate)

    context = await student_homework.get_homework_context(homework_id=str(hw.id), user=student, session=db_session)

    assert context.what_needs_understanding == f"You need more practice with {hw.gap_topic}."
    assert context.references[0].title == "Class notes"

    # Not cached - a failed attempt must not prevent retrying on the next call.
    await db_session.refresh(hw)
    assert hw.insight_generated_at is None


async def test_retries_generation_after_a_previous_failure(db_session, monkeypatch):
    hw, student = await _seed_homework_with_student(db_session)

    async def failing_generate(**kwargs):
        raise ColearnerInsightUnavailableError("down")

    monkeypatch.setattr(student_homework, "generate_homework_insight", failing_generate)
    await student_homework.get_homework_context(homework_id=str(hw.id), user=student, session=db_session)

    async def fake_generate(**kwargs):
        return _FAKE_INSIGHT

    monkeypatch.setattr(student_homework, "generate_homework_insight", fake_generate)
    context = await student_homework.get_homework_context(homework_id=str(hw.id), user=student, session=db_session)

    assert context.key_idea_title == _FAKE_INSIGHT["key_idea_title"]
    await db_session.refresh(hw)
    assert hw.insight_generated_at is not None


async def test_defaults_to_understand_when_no_bloom_scores_are_assessed_yet(db_session, monkeypatch):
    hw, student = await _seed_homework_with_student(db_session)

    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _FAKE_INSIGHT

    monkeypatch.setattr(student_homework, "generate_homework_insight", fake_generate)
    await student_homework.get_homework_context(homework_id=str(hw.id), user=student, session=db_session)

    assert captured["weak_bloom_level"] == "UNDERSTAND"


async def test_insight_client_forwards_the_students_token(monkeypatch):
    """Colearner's homework-insight route requires a signed-in caller, so
    core must pass the student's own bearer token through."""
    import httpx

    from src.services import colearner_insight_client

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_FAKE_INSIGHT)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(colearner_insight_client, "_base_url", lambda: "http://colearner")
    monkeypatch.setattr(
        colearner_insight_client.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    out = await colearner_insight_client.generate_homework_insight(
        topic="Electricity", grade="Grade 10", subject="Science", mastery_percent=40,
        weak_bloom_level="APPLY", textbook_context=None, token="student-token",
    )
    assert out == _FAKE_INSIGHT
    assert seen["auth"] == "Bearer student-token"
