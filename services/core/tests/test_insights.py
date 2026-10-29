"""GET /me/tests/{id}/insights (contract C4): numbers from stored rows,
cards for answers under 75, template-word fallback, students see only
their own. Real rows against the dev Postgres (rolled back); colearner's
words call is monkeypatched - no network/LLM."""

import asyncio
import importlib.util
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError

from shared.errors import NotFoundError

from src.api.routes import insights as insights_route
from src.domain.models import Plan
from src.services import insights
from src.domain.models import (
    AiAssessorSession,
    AiAssessorSessionRecord,
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Role,
    School,
    SchoolClass,
    StudentTestResult,
    StudentTestResultBloomScore,
    StudentTestResultTopicScore,
    Subject,
    Subtopic,
    Topic,
    User,
    UserStatus,
    VoiceTest,
    VoiceTestQuestion,
)


REVISION = "b4d8e2f6a1c9"


async def _ddl(db, work, attempts=5):
    """Runs `work` (sync, gets the connection) in a savepoint, retried when
    it deadlocks or waits too long on a lock held by another session."""
    for attempt in range(attempts):
        savepoint = await db.begin_nested()
        try:
            await db.execute(text("SET LOCAL lock_timeout = '3s'"))
            await db.run_sync(lambda s: work(s.connection()))
            await savepoint.commit()
            return
        except DBAPIError as exc:
            await savepoint.rollback()
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code not in ("40P01", "55P03") or attempt == attempts - 1:  # deadlock / lock timeout
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


@pytest_asyncio.fixture
async def db(db_session):
    """db_session with the insights_words column (rolled back with the test
    when the migration isn't applied to the dev DB yet)."""
    await _ddl(db_session, lambda conn: conn.execute(
        text("ALTER TABLE student_test_results ADD COLUMN IF NOT EXISTS insights_words jsonb")))
    return db_session


def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_insights_words.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w4_03_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "e5f1c7a2d9b3"

    def run(step):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                step()
        return work

    def columns(sync_session):
        return {c["name"] for c in inspect(sync_session.connection()).get_columns("student_test_results")}

    if "insights_words" not in await db_session.run_sync(columns):  # not applied to this DB yet
        await _ddl(db_session, run(module.upgrade))  # rolled back with the test's transaction
    assert "insights_words" in await db_session.run_sync(columns)
    await _ddl(db_session, run(module.downgrade))
    assert "insights_words" not in await db_session.run_sync(columns)


def _student():
    return User(firebase_uid=f"s-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
                display_name="Student", role=Role.STUDENT, school_id=None, status=UserStatus.ACTIVE)


async def _seed(db):
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db.add_all([school, subject])
    await db.flush()
    school_class = SchoolClass(school_id=school.id, grade=9, section=uuid.uuid4().hex[:8].upper())
    chapter = Chapter(subject_id=subject.id, name="Electricity")
    db.add_all([school_class, chapter])
    await db.flush()
    t_ohm, t_power = Topic(chapter_id=chapter.id, name="Ohm's Law"), Topic(chapter_id=chapter.id, name="Electric Power")
    db.add_all([t_ohm, t_power])
    await db.flush()
    sub = Subtopic(topic_id=t_ohm.id, name="Resistance")
    test = VoiceTest(class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
                     duration_minutes=20, completion_window_hours=48,
                     target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS)
    student = _student()
    db.add_all([sub, test, student])
    await db.flush()
    for order, (level, topic, points) in enumerate([
        (BloomLevel.REMEMBER, t_ohm, ["V = IR"]),
        (BloomLevel.APPLY, t_ohm, ["Use V = IR", "Units are ohms"]),
        (BloomLevel.ANALYZE, t_power, ["P = VI", "Power grows with current"]),
    ], start=1):
        db.add(VoiceTestQuestion(voice_test_id=test.id, order=order, bloom_level=level, topic_id=topic.id,
                                 subtopic_id=sub.id if order == 2 else None, concepts=[],
                                 original_text=f"Q{order}", reworded_text=f"Question {order}?",
                                 spoken_text=f"Question {order}", expected_points=points))
    result = StudentTestResult(test_id=test.id, student_id=student.id, mastery_percent=55)
    db.add(result)
    await db.flush()
    for level, pct in [(BloomLevel.REMEMBER, 90), (BloomLevel.APPLY, 40), (BloomLevel.ANALYZE, 35),
                       (BloomLevel.CREATE, None)]:
        db.add(StudentTestResultBloomScore(student_test_result_id=result.id, bloom_level=level, percent=pct))
    db.add_all([
        StudentTestResultTopicScore(student_test_result_id=result.id, topic_id=t_ohm.id, percent=65, answered=2),
        StudentTestResultTopicScore(student_test_result_id=result.id, topic_id=t_power.id, percent=35, answered=1),
    ])
    ai_session = AiAssessorSession(student_id=student.id, test_id=test.id, context_label="t", duration_seconds=60)
    db.add(ai_session)
    await db.flush()
    db.add(AiAssessorSessionRecord(session_id=ai_session.id, needs_review=False, report={"qa_transcripts": [
        {"question_number": 1, "score_percent": 90, "student_response": "V is IR", "points_covered": ["V = IR"]},
        {"question_number": 3, "score_percent": 35, "student_response": "power is current",
         "points_covered": ["power grows with current"]},
        {"question_number": 2, "score_percent": 40, "student_response": "multiply them", "points_covered": []},
    ]}))
    await db.flush()
    return test, student


async def _get(db, test, student, monkeypatch, words=None, exc=None):
    """The words are written in the background (AI-022): the first GET schedules the job, the test runs it
    to the end with the test's session, and a second GET reads what it stored."""
    calls, scheduled = [], []

    async def fake_words(facts, token):
        calls.append((facts, token))
        if exc:
            raise exc
        return words

    @asynccontextmanager
    async def same_session():
        yield db

    def schedule(result_id, fingerprint, facts, token):
        scheduled.append((result_id, fingerprint, facts, token))
        return True

    monkeypatch.setattr(insights, "_colearner_words", fake_words)
    monkeypatch.setattr(insights, "_schedule_words", schedule)

    async def route():
        return await insights_route.get_test_insights(test_id=str(test.id), user=student, session=db,
                                                      authorization="Bearer tok")

    out = await route()
    if scheduled:
        await insights._fill_words(*scheduled[-1], session_factory=same_session)
        out = await route()
    return out, calls


async def test_numbers_cards_and_words(db, monkeypatch):
    db_session = db
    test, student = await _seed(db_session)
    words = {"headline": "H", "key_ideas": {2: "Voltage is current times resistance."},
             "next_steps": [{"title": "Revise", "reference": "Ohm's Law"}] * 3, "recap_text": "Lovely effort."}
    out, calls = await _get(db_session, test, student, monkeypatch, words=words)

    assert out.mastery_percent == 55
    assert [(b.level, b.percent) for b in out.per_bloom] == [
        (BloomLevel.REMEMBER, 90), (BloomLevel.APPLY, 40), (BloomLevel.ANALYZE, 35)]
    assert [(t.name, t.percent, t.status) for t in out.topics] == [
        ("Electric Power", 35, "weak"), ("Ohm's Law", 65, "ok")]
    # only answers under 75, in question order; missed = expected not covered (case-insensitive)
    assert [c.question_number for c in out.cards] == [2, 3]
    assert out.cards[0].question_text == "Question 2?"
    assert out.cards[0].you_said == "multiply them"
    assert out.cards[0].missed_points == ["Use V = IR", "Units are ohms"]
    # each card's own score, so the app can pick the weakest as "One thing to fix" (V-005)
    assert [c.score_percent for c in out.cards] == [40, 35]
    assert out.cards[0].key_idea == "Voltage is current times resistance."
    assert out.cards[1].key_idea == "P = VI"  # colearner skipped it -> template
    assert out.cards[1].missed_points == []  # its only missed point is the key idea - shown once (AI-004)
    assert out.headline == "H" and out.recap_text == "Lovely effort."

    facts, token = calls[0]
    assert token == "tok"
    assert facts["cards"][0]["topic"] == "Ohm's Law" and facts["cards"][0]["subtopic"] == "Resistance"
    assert out.model_dump(by_alias=True)["cards"][0]["youSaid"] == "multiply them"

    # the words are stored on the result and reused - no second colearner call
    stored = (await db_session.execute(text(
        "SELECT insights_words FROM student_test_results WHERE test_id = :t"), {"t": test.id})).scalar_one()
    assert stored["words"]["headline"] == "H"
    again, second_calls = await _get(db_session, test, student, monkeypatch, words={"boom": 1})
    assert second_calls == []
    assert again.headline == "H" and again.generated_at == out.generated_at
    assert [c.key_idea for c in again.cards] == [c.key_idea for c in out.cards]


async def test_fingerprint_change_regenerates_words(db, monkeypatch):
    test, student = await _seed(db)
    words = {"headline": "H1", "key_ideas": {}, "next_steps": [], "recap_text": "R"}
    await _get(db, test, student, monkeypatch, words=words)
    result = (await db.execute(select(StudentTestResult).where(StudentTestResult.test_id == test.id))).scalar_one()
    result.mastery_percent = 60  # e.g. a regrade
    await db.flush()

    out, calls = await _get(db, test, student, monkeypatch, words={**words, "headline": "H2"})
    assert len(calls) == 1 and out.headline == "H2"
    _, calls = await _get(db, test, student, monkeypatch, words=words)
    assert calls == []


async def test_next_steps_padded_to_exactly_three(db, monkeypatch):
    test, student = await _seed(db)
    words = {"headline": "H", "key_ideas": {}, "recap_text": "R",
             "next_steps": [{"title": "Only one", "reference": "Ohm's Law"}]}
    out, _ = await _get(db, test, student, monkeypatch, words=words)
    assert [s.title for s in out.next_steps][0] == "Only one" and len(out.next_steps) == 3


def test_locked_when_plan_excludes_improvement_analysis(client, monkeypatch):
    from src.api import deps

    student = _student()
    student.school_id = uuid.uuid4()
    client.app.dependency_overrides[deps.get_current_app_user] = lambda: student
    client.app.dependency_overrides[deps.get_db_session] = lambda: None

    async def plan(_session, _school_id):
        return Plan(name="p", price_label="x", teacher_limit=1, student_limit=1, enabled_features=["voice_test"])

    monkeypatch.setattr(deps, "get_active_plan", plan)
    response = client.get(f"/api/v1/me/tests/{uuid.uuid4()}/insights")
    assert response.status_code == 403
    assert "improvement_analysis" in response.text


async def test_falls_back_to_template_words_when_colearner_fails(db, monkeypatch, caplog):
    db_session = db
    test, student = await _seed(db_session)
    out, _ = await _get(db_session, test, student, monkeypatch, exc=RuntimeError("down"))

    assert out.mastery_percent == 55
    assert out.headline == "You are strongest at Remember and can grow most at Analyze."
    assert [c.key_idea for c in out.cards] == ["Use V = IR", "P = VI"]
    assert [c.missed_points for c in out.cards] == [["Units are ohms"], []]  # the key idea isn't repeated as a bullet
    assert len(out.next_steps) == 3 and out.next_steps[0].reference == "Electric Power"
    assert "insights_words_fallback" in caplog.text
    stored = (await db_session.execute(text(
        "SELECT insights_words FROM student_test_results WHERE test_id = :t"), {"t": test.id})).scalar_one()
    assert stored is None  # template words aren't stored, so colearner is retried next time


async def test_404_without_own_result(db):
    db_session = db
    test, _owner = await _seed(db_session)
    other = _student()
    db_session.add(other)
    await db_session.flush()
    for test_id in (str(test.id), "not-a-uuid"):
        with pytest.raises(NotFoundError):
            await insights_route.get_test_insights(test_id=test_id, user=other, session=db_session,
                                                   authorization=None)


def test_teachers_are_forbidden(client):
    from src.api.deps import get_current_app_user, get_db_session

    teacher = User(firebase_uid="t", email="t@example.com", display_name="T", role=Role.TEACHER,
                   school_id=None, status=UserStatus.ACTIVE)
    client.app.dependency_overrides[get_current_app_user] = lambda: teacher
    client.app.dependency_overrides[get_db_session] = lambda: None
    assert client.get(f"/api/v1/me/tests/{uuid.uuid4()}/insights").status_code == 403


def test_topic_status_thresholds():
    assert [insights.topic_status(p) for p in (75, 74, 50, 49)] == ["strong", "ok", "ok", "weak"]


async def test_colearner_words_parses_the_colearner_response(monkeypatch):
    import httpx

    def handler(request):
        assert request.url.path == "/api/v1/insights"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={
            "headline": "H", "cards": [{"question_number": 2, "key_idea": "K"}],
            "next_steps": [{"title": str(i), "reference": "R"} for i in range(4)], "recap_text": "R"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(insights.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(insights, "get_settings", lambda: type("S", (), {"colearner_service_url": "http://colearner"})())

    words = await insights._colearner_words({"cards": []}, "tok")
    assert words["key_ideas"] == {2: "K"} and len(words["next_steps"]) == 3


def test_template_headline_never_names_one_level_as_both_best_and_worst():
    from src.domain.models import BloomLevel
    from src.services.insights import template_words

    def headline(*pcts):
        levels = [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND, BloomLevel.APPLY]
        return template_words([{"level": lv, "percent": p} for lv, p in zip(levels, pcts)], [], [], None)["headline"]

    assert headline(0, 0, 0) == "A tough one - let's build it up together."
    assert headline(60, 60, 60) == "You scored 60% at every level."
    assert headline(80, 40, 60) == "You are strongest at Remember and can grow most at Understand."


def test_you_said_is_the_stored_transcript_not_the_graders_note():
    """S-004: 'You said' showed the grader's annotation instead of the student's words."""
    qa = [{"question_number": 1, "score_percent": 10, "points_covered": [],
           "student_response": "I don't know. [followed by Tamil remarks, no scientific answer]"},
          {"question_number": 2, "score_percent": 20, "points_covered": [], "student_response": "graded words"}]
    answers = [{"question_number": 1, "first_answer": "I don't know,  enakku theriyala"}]
    cards = insights.build_cards(qa, {}, answers)
    assert [c["you_said"] for c in cards] == ["I don't know, enakku theriyala", "graded words"]


def test_rubric_cleanup_migration_drops_marking_notes():
    here = Path(__file__).resolve()
    path = next((r / "database/migrations/versions/a7c3e9b1d5f2_clean_rubric_marking_notes.py"
                 for r in (here.parents[1], *here.parents[2:4])
                 if (r / "database/migrations/versions/a7c3e9b1d5f2_clean_rubric_marking_notes.py").is_file()), None)
    if path is None:
        pytest.skip("database/ not available in this environment")
    spec = importlib.util.spec_from_file_location("rubric_cleanup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    points = ["A.", "B. Any two or three of these are enough.", "Any two of these are enough.",
              "In burning it is combustion, while this oxidation is endothermic. Any two are enough."]
    cleaned = module.clean(points)
    assert cleaned == ["A.", "B.", module.RIGHT] and module.clean(cleaned) == cleaned


async def test_the_screen_gets_template_words_at_once_and_the_model_runs_once_in_the_background(db, monkeypatch):
    """AI-022: a GET never waits for the model; asking again while it runs doesn't start a second job."""
    test, student = await _seed(db)
    started, release = [], asyncio.Event()

    async def fake_fill(result_id, fingerprint, facts, token, session_factory=None):
        started.append(result_id)
        await release.wait()  # the model is still thinking
        insights._in_flight.discard(result_id)

    monkeypatch.setattr(insights, "_fill_words", fake_fill)
    insights._in_flight.clear()
    insights._failed_at.clear()

    async def route():
        return await insights_route.get_test_insights(test_id=str(test.id), user=student, session=db,
                                                      authorization="Bearer tok")

    first = await route()
    assert first.words_pending is True
    assert first.headline == "You are strongest at Remember and can grow most at Analyze."  # the template
    assert first.model_dump(by_alias=True)["wordsPending"] is True
    again = await route()  # polled before the job is done
    assert again.words_pending is True
    release.set()
    await asyncio.sleep(0.01)
    assert len(started) == 1


async def test_after_a_failed_attempt_the_template_is_final_for_a_while(db, monkeypatch):
    test, student = await _seed(db)
    result = (await db.execute(select(StudentTestResult).where(StudentTestResult.test_id == test.id))).scalar_one()
    insights._in_flight.clear()
    insights._failed_at.clear()
    insights._failed_at[result.id] = time.monotonic()  # it just failed
    out = await insights_route.get_test_insights(test_id=str(test.id), user=student, session=db, authorization=None)
    assert out.words_pending is False and not insights._in_flight
