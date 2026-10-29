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


real_formats = subject_rules.challenge_formats


@pytest.fixture(autouse=True)
def _any_format_outside_maths(monkeypatch):
    """The challenge mechanics are tested on the seed's Science chapter with
    MCQs; the real rule (Maths: Fix Milo's homework only) still holds for Maths."""
    real = subject_rules.challenge_formats
    monkeypatch.setattr(service.subject_rules, "challenge_formats",
                        lambda name: real(name) if name == "Mathematics" else service.KINDS)


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
    assert out.status == "preparing" and out.title.startswith("Electricity challenge · ")  # dated, to tell tests apart
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
    assert not [t for t in (await task_routes.get_my_tasks(BackgroundTasks(), user=s.student, session=db_session)).tasks
                if t.kind == "quiz_test"]
    with pytest.raises(NotFoundError):
        await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)

    sent = []

    async def notify(session, ids, title, body, data=None):
        sent.append((set(ids), title))
    monkeypatch.setattr(service.push, "notify", notify)
    out = await routes.publish_quiz_test(created.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)
    assert out.status == "published" and sent == [({k.id for k in s.students}, f"New challenge: {created.title}")]

    [task] = [t for t in (await task_routes.get_my_tasks(BackgroundTasks(), user=s.student, session=db_session)).tasks
              if t.kind == "quiz_test"]
    assert [(st.kind, st.status) for st in task.steps] == [
        ("test", "current"), ("homework", "locked"), ("test_recheck", "locked")]
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
    [listed] = (await routes.list_my_quiz_tests(user=s.teacher, session=db_session)).items
    assert (listed.done_count, listed.student_count) == (1, 2)  # "1 of 2 done" on the teacher's list
    assert results.weakest_level == "APPLY" and len(results.needs_help) == 1
    assert sorted(r.status for r in results.students) == ["done", "not_started"]

    # like a check-in: the homework, then a second look in the same format
    with pytest.raises(ConflictError):  # not before the homework
        await routes.start_my_quiz_test(created.id, attempt=2, user=s.student, session=db_session)
    first = await db_session.get(service.DailyPracticeSession, service.uuid.UUID(attempt.id))
    first.analysis = {**first.analysis, "homework_skipped": True}  # (no dataset to make it from)
    await db_session.flush()
    [task] = [t for t in (await task_routes.get_my_tasks(BackgroundTasks(), user=s.student, session=db_session)).tasks
              if t.kind == "quiz_test"]
    assert task.steps[2].kind == "test_recheck" and task.steps[2].status == "current"
    look = await routes.start_my_quiz_test(created.id, attempt=2, user=s.student, session=db_session)
    assert look.id != attempt.id and look.items and {i.kind for i in look.items} == {"mcq"}  # the test's format
    assert not {i.text for i in look.items} & {i.text for i in attempt.items}  # fresh questions
    await _answer(db_session, s, look, {R, U, B.APPLY, B.ANALYZE, B.EVALUATE, B.CREATE})
    await quiz_routes.finish_my_topic_quiz(look.id, background_tasks=BackgroundTasks(), user=s.student,
                                           session=db_session)
    [task] = [t for t in (await task_routes.get_my_tasks(BackgroundTasks(), user=s.student, session=db_session)).tasks
              if t.kind == "quiz_test"]
    assert (task.state, task.result) == ("done", "Mastered")
    results = await routes.get_quiz_test_results(created.id, user=s.teacher, session=db_session)
    assert results.average_percent == 25  # the teacher's results are the first try

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


async def test_preparing_never_sticks_on_a_hang_an_error_or_a_restart(db_session, monkeypatch):
    """T-009: the teacher saw "Picking questions..." for 7+ minutes - prepare() logged an
    error (or hung on the model) and left the test 'preparing' for good."""
    import asyncio

    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)

    async def hang(*a, **k):
        await asyncio.sleep(30)

    async def boom(*a, **k):
        raise KeyError("text")

    monkeypatch.setattr(service, "PREPARE_TIMEOUT_SECONDS", 0.05, raising=False)
    for fill in (boom, hang):
        monkeypatch.setattr(service.daily_practice, "_fill", fill)
        created = await _create(db_session, s, monkeypatch)
        assert (await routes.get_quiz_test(created.id, user=s.teacher, session=db_session)).status == "failed"
        await db_session.delete(await db_session.get(QuizTest, service.uuid.UUID(created.id)))

    # the server restarted mid-prepare: shown as failed once it can't finish
    test = QuizTest(status="preparing", created_at=datetime.now(timezone.utc) - timedelta(minutes=11))
    assert service.status_of(test) == "failed"
    test.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    assert service.status_of(test) == "preparing"


async def test_a_maths_challenge_is_fix_milo_or_who_is_right(db_session, monkeypatch):
    """T-019: Who is right? joins Fix Milo's homework; MCQs and Build your own stay check-ins."""
    assert subject_rules.challenge_formats("Mathematics") == ("fix_steps", "who_is_right")
    assert real_formats("Science") == ("voice", "fix_steps")
    from src.domain.models import Chapter, Subject

    s = await _seed(db_session)
    subject = await db_session.get(Subject, (await db_session.get(Chapter, s.electricity.id)).subject_id)
    subject.name = "Mathematics"
    await db_session.flush()
    for other in ("mcq", "make_your_own"):
        with pytest.raises(ValidationError, match=other):
            await _create(db_session, s, monkeypatch, mix={other: 2, "fix_steps": 1})


async def test_a_one_type_challenge_keeps_its_type_and_skips_repeats(db_session, monkeypatch):
    """Fix Milo only, over two topics where one has no Fix Milo questions: both
    slots stay Fix Milo (the other topic's), and a board question that appears
    twice (two years' papers) is used once."""
    from src.domain.models import Chapter, PracticeQuestion, Subject
    from tests.test_question_types import FIX

    s = await _seed(db_session)
    subject = await db_session.get(Subject, (await db_session.get(Chapter, s.electricity.id)).subject_id)
    subject.name = "Mathematics"
    for text in ("Find the HCF of 12 and 18.", "Find the HCF of 12 and 18 !", "Find the LCM of 4 and 6."):
        db_session.add(PracticeQuestion(chapter_id=s.electricity.id, topic_id=s.ohm.id, bloom_level=B.EVALUATE,
                                        kind="fix_steps", text=text, spec=FIX, expected_points=[], misconceptions=[],
                                        origin="bank", used_count=0))
    await db_session.flush()

    created = await _create(db_session, s, monkeypatch, mix={"fix_steps": 2})
    detail = await routes.get_quiz_test(created.id, user=s.teacher, session=db_session)
    assert [q.kind for q in detail.questions] == ["fix_steps", "fix_steps"]
    texts = [q.text for q in detail.questions]
    assert "Find the LCM of 4 and 6." in texts
    assert sum(t.startswith("Find the HCF of 12 and 18") for t in texts) == 1  # either copy, never both


async def test_teacher_home_only_shows_the_teachers_own_subject(db_session):
    """Another teacher's chat with Milo in the same class (another subject) isn't this teacher's headline."""
    from src.api.routes import teacher_dashboard
    from src.domain.models import AssignmentTargetMode, Chapter, Subject, VoiceTest

    s = await _seed(db_session)
    other = Subject(name=f"Biology-{service.uuid.uuid4()}")
    db_session.add(other)
    await db_session.flush()
    chapter = Chapter(subject_id=other.id, name="Life Processes", order_index=1, grade=10)
    db_session.add(chapter)
    await db_session.flush()
    db_session.add(VoiceTest(class_id=s.cls.id, subject_id=other.id, chapter_id=chapter.id, duration_minutes=8,
                             completion_window_hours=24, target_mode=AssignmentTargetMode.WHOLE_CLASS))
    await db_session.flush()
    dash = await teacher_dashboard.get_teacher_dashboard(class_id=str(s.cls.id), user=s.teacher, session=db_session)
    assert dash.headline_test is None


async def test_progress_counts_check_ins_and_challenges_per_subject(db_session):
    """Progress isn't only chats with Milo: a finished check-in shows under its
    subject, with its thinking levels and the weakest one to work on."""
    from src.services import student_progress

    s = await _seed(db_session)
    db_session.add(service.DailyPracticeSession(
        student_id=s.student.id, practice_date=service.daily_practice.today_ist(), chapter_id=s.electricity.id,
        topic_id=s.ohm.id, attempt=1, status="completed", xp_awarded=0, score_percent=40,
        per_bloom={"REMEMBER": 100, "APPLY": 25}, outcome="homework"))
    await db_session.flush()
    [subject] = await student_progress.by_subject(db_session, s.student)
    assert subject["subject_label"].startswith("Science") and (subject["count"], subject["score_percent"]) == (1, 40)
    assert (subject["focus_level"], subject["focus_percent"]) == (B.APPLY, 25)
    assert [r["kind"] for r in subject["results"]] == ["Topic checkpoint"]


async def test_badges_come_from_check_ins_and_challenges_too(db_session):
    """A Maths student never chats with Milo - a Fix Milo challenge at 80%+ in
    Judge it (Evaluate) still earns the Judge badge."""
    from src.services import gamification

    s = await _seed(db_session)
    db_session.add(service.DailyPracticeSession(
        student_id=s.student.id, practice_date=service.daily_practice.today_ist(), chapter_id=s.electricity.id,
        attempt=1, status="completed", xp_awarded=0, score_percent=90, per_bloom={"EVALUATE": 90},
        outcome="mastered", completed_at=datetime.now(timezone.utc)))
    await db_session.flush()
    assert set(await gamification.sync_badges(db_session, s.student.id)) == {"badge-judge"}


async def test_like_a_voice_co_learner_a_window_from_opening_and_chosen_students(db_session, monkeypatch):
    """Fix Milo gets the voice co-learner's options: the deadline is the window
    counted from when it opens, and only the chosen students get it."""
    s = await _seed(db_session, students=2)
    for topic in (s.ohm, s.power):
        await _teach(db_session, s, topic)
        await _pools(db_session, topic)
    chosen, other = s.students[0], s.students[1]
    created = await _create(db_session, s, monkeypatch, completion_window_hours=24, student_ids=[str(chosen.id)])
    assert created.completion_window_hours == 24 and created.due_at is None  # not open yet: no deadline

    out = await routes.publish_quiz_test(created.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)
    assert abs((out.due_at - out.opens_at).total_seconds() - 24 * 3600) < 1

    async def mine(student):
        return [t for t in (await task_routes.get_my_tasks(BackgroundTasks(), user=student, session=db_session)).tasks
                if t.kind == "quiz_test"]
    assert len(await mine(chosen)) == 1 and await mine(other) == []
    results = await routes.get_quiz_test_results(created.id, user=s.teacher, session=db_session)
    assert results.student_count == 1
    [listed] = (await routes.list_my_quiz_tests(user=s.teacher, session=db_session)).items
    assert listed.student_count == 1


async def test_one_challenge_cycle_per_chapter_per_class(db_session, monkeypatch):
    s = await _seed(db_session, students=1)
    for topic in (s.ohm, s.power):
        await _teach(db_session, s, topic)
        await _pools(db_session, topic)
    _stub_analysis(monkeypatch, [])
    monkeypatch.setattr(service.push, "notify", lambda *a, **k: _noop())
    first = await _create(db_session, s, monkeypatch)
    second = await _create(db_session, s, monkeypatch)  # a second draft is fine - nothing is open yet

    await routes.publish_quiz_test(first.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)
    with pytest.raises(ConflictError):  # the chapter's cycle is running - not another
        await routes.publish_quiz_test(second.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)
    with pytest.raises(ConflictError):
        await _create(db_session, s, monkeypatch)
    # one per format: a Speak with Milo on the same chapter is still free
    from src.services import challenge_rules
    assert await challenge_rules.taken_formats(db_session, s.cls.id, s.electricity.id) == {"fix_steps"}
    await challenge_rules.ensure_one_per_chapter(db_session, s.cls.id, s.electricity.id, "voice")


async def _noop():
    return None


async def test_the_teachers_report_says_whats_in_and_out_and_what_to_do(db_session, monkeypatch):
    from src.api.routes import challenge_report as report_routes

    s = await _seed(db_session, students=2)
    for topic in (s.ohm, s.power):
        await _teach(db_session, s, topic)
        await _pools(db_session, topic)
    _stub_analysis(monkeypatch, [])
    monkeypatch.setattr(service.push, "notify", lambda *a, **k: _noop())
    created = await _create(db_session, s, monkeypatch)
    await routes.publish_quiz_test(created.id, QuizTestScheduleIn(), user=s.teacher, session=db_session)
    attempt = await routes.start_my_quiz_test(created.id, user=s.student, session=db_session)
    await _answer(db_session, s, attempt, {R, U})
    await quiz_routes.finish_my_topic_quiz(attempt.id, background_tasks=BackgroundTasks(), user=s.student,
                                           session=db_session)

    report = await report_routes.get_challenge_report(test_id=None, quiz_test_id=created.id, user=s.teacher,
                                                      session=db_session)
    assert (report.kind, report.assigned, report.finished, report.average_percent) == ("fix", 2, 1, 25)
    assert len(report.not_started) == 1
    assert report.topics and report.topics[0].percent <= report.topics[-1].percent  # weakest topic first
    assert report.topics[0].struggling == [s.student.display_name]
    assert report.questions and report.questions[0].percent == 0  # the ones nobody got, first
    me = next(st for st in report.students if st.student_id == str(s.student.id))
    assert me.status == "done" and me.score == 25 and me.weakest_skill == "Use it"
    assert any(a.startswith("Re-teach") for a in report.actions)
    assert any("haven't started" in a for a in report.actions)

    stranger = (await _seed(db_session)).teacher
    with pytest.raises(NotFoundError):
        await report_routes.get_challenge_report(test_id=None, quiz_test_id=created.id, user=stranger,
                                                 session=db_session)
