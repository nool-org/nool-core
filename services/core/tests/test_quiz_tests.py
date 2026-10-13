"""Teacher-made written tests: plan from the mix, questions picked once in the
background, reviewed with answers, published now or scheduled, taken by
students from their Tests tab, analysed (homework on the weakest topic),
results per student - and Maths can't have voice tests.

Real rows against the dev Postgres via db_session (seeded like the topic
quiz tests); generation stubbed."""

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks

from shared.errors import ConflictError, NotFoundError, ValidationError
from src.api.routes import quiz_tests as routes
from src.api.routes import student_tasks as task_routes
from src.api.routes import topic_quizzes as quiz_routes
from src.api.schemas.quiz_tests import QuizTestCreateIn, QuizTestScheduleIn
from src.domain.models import BloomLevel as B
from src.domain.models import QuizTest
from src.services import quiz_tests as service
from src.services import subject_rules
from tests.test_topic_quiz import R, U, _answer, _pools, _seed, _stub_analysis, _teach


def test_plan_spreads_topics_and_sets_each_types_level():
    from types import SimpleNamespace

    t1, t2 = SimpleNamespace(name="a"), SimpleNamespace(name="b")
    slots = service.plan([t1, t2], {"mcq": 5, "who_is_right": 1, "make_your_own": 1})
    assert [(s.kind, s.bloom_level) for s in slots] == [
        ("mcq", B.APPLY), ("mcq", B.APPLY), ("mcq", B.UNDERSTAND), ("mcq", B.APPLY), ("mcq", B.ANALYZE),
        ("who_is_right", B.ANALYZE), ("make_your_own", B.CREATE)]
    assert [s.topic for s in slots[:3]] == [t1, t2, t1]
    for bad in ({"mcq": 0}, {"mcq": 21}, {"essay": 2}):
        with pytest.raises(ValidationError):
            service.plan([t1], bad)
    assert not subject_rules.voice_tests_allowed("Mathematics") and subject_rules.voice_tests_allowed("Science")


@contextlib.asynccontextmanager
async def _own_session(db):
    class Proxy:  # the background task's own session -> the test's, commit = flush
        def __getattr__(self, name):
            return getattr(db, name)

        async def commit(self):
            await db.flush()

    yield Proxy()


async def _create(db, s, monkeypatch, mix=None, **kw):
    monkeypatch.setattr("src.repositories.get_session", lambda: _own_session(db))
    out = await routes.create_quiz_test(str(s.cls.id), QuizTestCreateIn(
        chapter_id=str(s.electricity.id), topic_ids=[str(s.ohm.id), str(s.power.id)],
        mix=mix or {"mcq": 4}, **kw), background_tasks=BackgroundTasks(), user=s.teacher, session=db)
    assert out.status == "preparing" and out.title.startswith("Electricity test · ")  # dated, to tell tests apart
    await service.prepare(service.uuid.UUID(out.id))
    return out


async def test_teacher_creates_reviews_publishes_and_sees_results(db_session, monkeypatch):
    s = await _seed(db_session, students=2)
    for topic in (s.ohm, s.power):
        await _teach(db_session, s, topic)
        await _pools(db_session, topic)
    _stub_analysis(monkeypatch, [])
    created = await _create(db_session, s, monkeypatch, due_at=datetime.now(timezone.utc) + timedelta(days=2))

    detail = await routes.get_quiz_test(created.id, user=s.teacher, session=db_session)
    assert detail.status == "ready" and detail.question_count == 4
    assert [q.bloom_level for q in detail.questions] == ["APPLY", "APPLY", "UNDERSTAND", "APPLY"]
    assert all(q.correct_index is not None for q in detail.questions)  # the teacher sees the answers

    # not open yet: students don't see it and can't start it
    assert not [t for t in (await task_routes.get_my_tasks(user=s.student, session=db_session)).tasks
                if t.kind == "quiz_test"]
    with pytest.raises(NotFoundError):
        await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)

    sent = []

    async def notify(session, ids, title, body, data=None):
        sent.append((set(ids), title))
    monkeypatch.setattr(service.push, "notify", notify)
    out = await routes.publish_quiz_test(created.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)
    assert out.status == "published" and sent == [({k.id for k in s.students}, f"New test: {created.title}")]

    [task] = [t for t in (await task_routes.get_my_tasks(user=s.student, session=db_session)).tasks
              if t.kind == "quiz_test"]
    assert [(st.kind, st.status) for st in task.steps] == [("test", "current"), ("homework", "locked")]
    assert task.steps[0].due_at is not None

    attempt = await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)
    assert attempt.topic_name == created.title and attempt.quiz_test_id == created.id and len(attempt.items) == 4
    assert (await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)).id == attempt.id
    await _answer(db_session, s, attempt, {R, U})
    done = await quiz_routes.finish_my_topic_quiz(attempt.id, background_tasks=BackgroundTasks(), user=s.student,
                                                  session=db_session)
    assert done.analysis.score_percent == 25 and done.analysis.outcome == "homework"  # 3 of 4 are Apply problems

    results = await routes.get_quiz_test_results(created.id, user=s.teacher, session=db_session)
    assert (results.student_count, results.done, results.average_percent) == (2, 1, 25)
    assert results.weakest_level == "APPLY" and len(results.needs_help) == 1
    assert sorted(r.status for r in results.students) == ["done", "not_started"]

    stranger = (await _seed(db_session)).teacher
    with pytest.raises(NotFoundError):
        await routes.get_quiz_test(created.id, user=stranger, session=db_session)


async def test_a_scheduled_test_opens_at_its_time(db_session, monkeypatch):
    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    await _pools(db_session, s.power)
    created = await _create(db_session, s, monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    out = await routes.publish_quiz_test(created.id, QuizTestScheduleIn(opens_at=later), user=s.teacher,
                                         session=db_session)
    assert out.status == "scheduled" and out.opens_at == later
    with pytest.raises(NotFoundError):
        await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)
    test = await db_session.get(QuizTest, service.uuid.UUID(created.id))
    test.published_at = datetime.now(timezone.utc) - timedelta(minutes=1)  # time passes
    await db_session.flush()
    assert len((await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)).items) == 4


async def test_publishing_needs_ready_questions_and_maths_has_no_voice_tests(db_session, monkeypatch):
    from src.api.routes import voice_test as voice_routes
    from src.api.schemas.voice_test import CreateTestIn
    from src.domain.models import Chapter, Subject

    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    monkeypatch.setattr(service.daily_practice.generation_client, "is_configured", lambda: False)
    created = await _create(db_session, s, monkeypatch)  # no questions anywhere
    assert (await routes.get_quiz_test(created.id, user=s.teacher, session=db_session)).status == "failed"
    with pytest.raises(ConflictError):
        await routes.publish_quiz_test(created.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)

    subject = await db_session.get(Subject, (await db_session.get(Chapter, s.electricity.id)).subject_id)
    subject.name, subject.grade = "Mathematics", None
    await db_session.flush()
    body = CreateTestIn.model_validate({
        "classId": str(s.cls.id), "subjectId": str(subject.id), "chapterId": str(s.electricity.id),
        "bloomLevels": ["REMEMBER"], "durationMinutes": 10, "completionWindowHours": 24,
        "target": {"mode": "WHOLE_CLASS"}})
    with pytest.raises(ValidationError, match="written tests"):
        await voice_routes.create_test(body, background_tasks=BackgroundTasks(), user=s.teacher, _feature=s.teacher,
                                       session=db_session)
