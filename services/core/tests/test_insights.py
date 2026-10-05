"""GET /me/tests/{id}/insights (contract C4): numbers from stored rows,
cards for answers under 75, template-word fallback, students see only
their own. Real rows against the dev Postgres (rolled back); colearner's
words call is monkeypatched - no network/LLM."""

import uuid

import pytest

from shared.errors import NotFoundError

from src.api.routes import insights as insights_route
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


@pytest.fixture(autouse=True)
def _empty_cache():
    insights._WORDS_CACHE.clear()


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
    calls = []

    async def fake_words(facts, token):
        calls.append((facts, token))
        if exc:
            raise exc
        return words

    monkeypatch.setattr(insights, "_colearner_words", fake_words)
    out = await insights_route.get_test_insights(test_id=str(test.id), user=student, session=db,
                                                 authorization="Bearer tok")
    return out, calls


async def test_numbers_cards_and_words(db_session, monkeypatch):
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
    assert out.cards[1].missed_points == ["P = VI"]
    assert out.cards[0].key_idea == "Voltage is current times resistance."
    assert out.cards[1].key_idea == "P = VI"  # colearner skipped it -> template
    assert out.headline == "H" and out.recap_text == "Lovely effort."

    facts, token = calls[0]
    assert token == "tok"
    assert facts["cards"][0]["topic"] == "Ohm's Law" and facts["cards"][0]["subtopic"] == "Resistance"
    assert out.model_dump(by_alias=True)["cards"][0]["youSaid"] == "multiply them"

    # second request is served from the cache, no second colearner call
    _, second_calls = await _get(db_session, test, student, monkeypatch, words=words)
    assert second_calls == []


async def test_falls_back_to_template_words_when_colearner_fails(db_session, monkeypatch, caplog):
    test, student = await _seed(db_session)
    out, _ = await _get(db_session, test, student, monkeypatch, exc=RuntimeError("down"))

    assert out.mastery_percent == 55
    assert out.headline == "You are strongest at Remember and can grow most at Analyze."
    assert [c.key_idea for c in out.cards] == ["Use V = IR", "P = VI"]
    assert len(out.next_steps) == 3 and out.next_steps[0].reference == "Electric Power"
    assert "insights_words_fallback" in caplog.text
    assert not insights._WORDS_CACHE  # failures are retried next time


async def test_404_without_own_result(db_session):
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
