"""Topic quizzes: quiz levels, per-level scores and lagging levels, then the
loop end to end - quiz -> analysis -> homework -> re-check -> mastered or
flagged for the teacher - and the teacher's chapter summary.

Real rows against the dev Postgres via db_session (seeded like daily
practice); the generation service is stubbed - no model calls."""

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select

from shared.errors import ConflictError, NotFoundError
from src.api.routes import daily_practice as practice_routes
from src.api.routes import topic_quizzes as routes
from src.api.schemas.daily_practice import PracticeMcqAnswerIn
from src.api.schemas.topic_quiz import QuizStartIn
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Homework,
    HomeworkDifficulty,
    HomeworkStatus,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
)
from src.services import topic_quiz as service
from tests.test_daily_practice import _pool, _seed, _teach

B = BloomLevel
R, U, A, AN = "REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE"


def test_quiz_levels():
    assert service.quiz_levels(1) == [B.REMEMBER, B.REMEMBER, B.UNDERSTAND, B.UNDERSTAND,
                                      B.APPLY, B.APPLY, B.ANALYZE, B.ANALYZE]
    assert service.quiz_levels(2, [A]) == [B.APPLY, B.APPLY, B.REMEMBER, B.UNDERSTAND, B.ANALYZE, B.APPLY]
    assert service.quiz_levels(2, [AN, A, U]) == [B.ANALYZE, B.ANALYZE, B.APPLY, B.APPLY, B.UNDERSTAND,
                                                  B.UNDERSTAND]


def test_quiz_slots_add_two_spoken_answers():
    first = service.quiz_slots(1)
    assert [k for k, _ in first] == ["mcq"] * 8 + ["voice"] * 2
    assert [lv for k, lv in first if k == "voice"] == [B.UNDERSTAND, B.APPLY]
    assert [lv for k, lv in service.quiz_slots(2, [AN, A]) if k == "voice"] == [B.ANALYZE, B.APPLY]
    assert [lv for k, lv in service.quiz_slots(2, [A]) if k == "voice"] == [B.APPLY, B.UNDERSTAND]
    assert len(service.quiz_slots(2, [A])) == 8


def test_maths_quiz_mixes_in_its_own_question_types():
    first = service.quiz_slots(1, subject="Mathematics")
    assert first == [("mcq", B.REMEMBER), ("mcq", B.UNDERSTAND)] + [("mcq", B.APPLY)] * 4 + [
        ("who_is_right", B.ANALYZE), ("fix_steps", B.EVALUATE), ("make_your_own", B.CREATE)]  # no spoken answers in Maths
    recheck = service.quiz_slots(2, ["CREATE", "APPLY"], subject="Mathematics")
    assert [k for k, _ in recheck] == ["mcq"] * 4 + ["make_your_own"]
    assert recheck[0] == ("mcq", B.APPLY)
    assert [k for k, _ in service.quiz_slots(2, ["APPLY"], subject="Mathematics")][-1] == "who_is_right"
    assert service.quiz_slots(1, subject="Science") == service.quiz_slots(1)  # no mix: unchanged


def test_scores_lagging_and_outcome():
    scores = service.per_bloom([(B.APPLY, False), (B.REMEMBER, True), (B.REMEMBER, False), (B.APPLY, False),
                                (B.UNDERSTAND, True)])
    assert scores == {R: 50, U: 100, A: 0}
    assert service.lagging_levels(scores) == [A, R]
    assert service.lagging_levels({R: 50, A: 50}) == [R, A]  # tie: the lower level first
    assert list(service.ordered({A: 0, AN: 0, R: 100, U: 100})) == [R, U, A, AN]  # JSONB's order -> Bloom's
    assert service.ordered(None) is None
    assert service.outcome_of(1, []) == service.outcome_of(2, []) == "mastered"
    assert service.outcome_of(1, [A]) == "homework" and service.outcome_of(2, [A]) == "needs_teacher"


async def _pools(db, topic):
    for level in service.LEVELS:
        for q in await _pool(db, topic, 4, bloom_level=level):
            q.text += f" {level.value}"
    for level in service.VOICE_LEVELS + [B.ANALYZE]:
        for q in await _pool(db, topic, 2, kind="voice", bloom_level=level):
            q.text += f" {level.value}"
    await db.flush()


def _stub_analysis(monkeypatch, calls):
    async def analysis(payload):
        calls.append(payload)
        return {"summary": "Good recall.", "lagging_meaning": "Using it is hard.", "need_to_know": ["V = IR"],
                "next_step": "Do the homework."}
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)
    monkeypatch.setattr(service.generation_client, "quiz_analysis", analysis)


async def _answer(db, s, quiz, right_levels):
    for item in quiz.items:  # MCQs only - spoken answers are skipped (they don't count)
        if item.kind != "mcq":
            continue
        option = 1 if item.bloom_level in right_levels else 0  # pool questions: index 1 is right
        await practice_routes.answer_my_daily_practice_mcq(quiz.id, item.id, PracticeMcqAnswerIn(option_index=option),
                                                           user=s.student, session=db)


async def _start(db, s, topic, attempt=1):
    return await routes.start_my_topic_quiz(str(topic.id), QuizStartIn(attempt=attempt), user=s.student, session=db)


async def _finish(db, s, quiz, tasks=None):
    return await routes.finish_my_topic_quiz(quiz.id, background_tasks=tasks or BackgroundTasks(), user=s.student,
                                             session=db)


async def _stage(db, s, topic):
    loops = (await routes.get_my_topic_loops(None, None, user=s.student, session=db)).topics
    return next(t for t in loops if t.topic_id == str(topic.id))


async def _homework_done(db, s, quiz_id, status=StudentHomeworkStatus.COMPLETED):
    hw = Homework(test_id=None, topic_id=s.ohm.id, class_id=s.cls.id, gap_topic="Ohm's law", gap_mastery_percent=50,
                  total_questions=5, difficulty=HomeworkDifficulty.MIXED,
                  target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS, status=HomeworkStatus.ASSIGNED,
                  assigned_count=1)
    db.add(hw)
    await db.flush()
    db.add(StudentHomeworkProgress(homework_id=hw.id, student_id=s.student.id, status=status))
    quiz = await service.own_quiz(db, quiz_id, s.student)
    quiz.homework_id = hw.id
    await db.flush()
    return hw


async def test_the_loop_quiz_analysis_homework_recheck_mastered(db_session, monkeypatch):
    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    calls = []
    _stub_analysis(monkeypatch, calls)

    assert (await _stage(db_session, s, s.ohm)).stage == "quiz"
    quiz = await _start(db_session, s, s.ohm)
    assert [(i.kind, i.bloom_level) for i in quiz.items] == [("mcq", lv) for lv in (R, R, U, U, A, A, AN, AN)] + [
        ("voice", U), ("voice", A)]
    assert quiz.analysis is None and all(i.result is None for i in quiz.items)
    assert all(i.options for i in quiz.items if i.kind == "mcq") and not quiz.items[-1].options
    assert (await _start(db_session, s, s.ohm)).id == quiz.id  # the same quiz on a second open
    with pytest.raises(ConflictError):
        await _finish(db_session, s, quiz)  # nothing answered yet

    await _answer(db_session, s, quiz, {R, U})
    tasks = BackgroundTasks()
    done = await _finish(db_session, s, quiz, tasks)
    a = done.analysis
    assert a.score_percent == 50 and a.per_bloom == {R: 100, U: 100, A: 0, AN: 0}
    assert a.lagging == [A, AN] and a.outcome == "homework" and a.need_to_know == ["V = IR"]
    assert calls[0]["lagging_level"] == A and len(calls[0]["missed"]) == 4 and calls[0]["attempt"] == 1
    assert [t.func.__name__ for t in tasks.tasks] == ["build_homework"]
    again = await _finish(db_session, s, quiz)  # stored: no second analysis
    assert again.analysis == a and len(calls) == 1

    assert (await _stage(db_session, s, s.ohm)).stage == "homework_pending"
    with pytest.raises(ConflictError):
        await _start(db_session, s, s.ohm, attempt=2)
    hw = await _homework_done(db_session, s, quiz.id, StudentHomeworkStatus.IN_PROGRESS)
    loop = await _stage(db_session, s, s.ohm)
    assert loop.stage == "homework" and loop.homework_id == str(hw.id) and loop.quiz_score == 50
    progress = (await db_session.execute(select(StudentHomeworkProgress).where(
        StudentHomeworkProgress.homework_id == hw.id))).scalar_one()
    progress.status = StudentHomeworkStatus.COMPLETED
    await db_session.flush()
    assert (await _stage(db_session, s, s.ohm)).stage == "recheck"

    recheck = await _start(db_session, s, s.ohm, attempt=2)
    assert [(i.kind, i.bloom_level) for i in recheck.items] == [("mcq", lv) for lv in (A, A, AN, AN, R, U)] + [
        ("voice", A), ("voice", AN)]
    assert not {i.text for i in recheck.items} & {i.text for i in quiz.items}  # fresh questions
    await _answer(db_session, s, recheck, {R, U, A, AN})
    result = (await _finish(db_session, s, recheck)).analysis
    assert result.outcome == "mastered" and result.previous_per_bloom == a.per_bloom
    assert calls[1]["attempt"] == 2 and calls[1]["previous_per_bloom"] == a.per_bloom
    loop = await _stage(db_session, s, s.ohm)
    assert loop.stage == "mastered" and loop.recheck_score == 100


async def test_still_weak_after_the_recheck_flags_the_teacher(db_session, monkeypatch):
    s = await _seed(db_session, students=2)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    _stub_analysis(monkeypatch, [])
    quiz = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, quiz, {R, U, AN})
    await _finish(db_session, s, quiz)
    await _homework_done(db_session, s, quiz.id)
    recheck = await _start(db_session, s, s.ohm, attempt=2)
    await _answer(db_session, s, recheck, {R, U, AN})
    assert (await _finish(db_session, s, recheck)).analysis.outcome == "needs_teacher"
    assert (await _stage(db_session, s, s.ohm)).stage == "needs_teacher"

    summary = (await routes.get_class_topic_quizzes(str(s.cls.id), str(s.electricity.id), user=s.teacher,
                                                    session=db_session)).topics
    [ohm] = summary
    assert (ohm.student_count, ohm.quizzed, ohm.mastered, ohm.average_percent) == (2, 1, 0, 75)
    assert ohm.weakest_level == A and ohm.per_bloom[A] == 0
    assert [(n.name, n.lagging) for n in ohm.needs_teacher] == [("Kid 0", A)]


async def test_nothing_lagging_is_mastered_at_once_without_homework(db_session, monkeypatch):
    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: False)

    async def down(payload):
        raise service.generation_client.GenerationServiceUnavailableError("down")
    monkeypatch.setattr(service.generation_client, "quiz_analysis", down)
    quiz = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, quiz, {R, U, A, AN})
    tasks = BackgroundTasks()
    a = (await _finish(db_session, s, quiz, tasks)).analysis
    assert a.outcome == "mastered" and a.lagging == [] and "sorted" in a.summary  # plain-words fallback
    assert tasks.tasks == []
    assert (await _stage(db_session, s, s.ohm)).stage == "mastered"


async def test_only_taught_topics_and_own_quizzes(db_session):
    s = await _seed(db_session, students=2)
    with pytest.raises(NotFoundError):
        await _start(db_session, s, s.ohm)  # not taught yet
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    quiz = await _start(db_session, s, s.ohm)
    with pytest.raises(NotFoundError):
        await routes.get_my_topic_quiz(quiz.id, user=s.students[1], session=db_session)
    with pytest.raises(NotFoundError):  # a daily practice session isn't a topic quiz
        daily = await practice_routes.get_my_daily_practice(user=s.student, session=db_session)
        await routes.get_my_topic_quiz(daily.session_id or "nope", user=s.student, session=db_session)


async def test_lagging_quiz_builds_personal_homework_on_the_lagging_levels(db_session, monkeypatch):
    import contextlib

    from src.services import homework_builder, push
    from src.services.homework_builder import QuestionDraft

    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    _stub_analysis(monkeypatch, [])
    quiz = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, quiz, {R, U, AN})
    await _finish(db_session, s, quiz)

    class Proxy:  # the background task's own session -> the test's, commit = flush
        def __getattr__(self, name):
            return getattr(db_session, name)

        async def commit(self):
            await db_session.flush()

    @contextlib.asynccontextmanager
    async def own_session():
        yield Proxy()

    asked, sent = {}, []

    async def build(session, **kw):
        asked.update(kw)
        return [QuestionDraft(bloom_level=level, text=f"q {n}", answer="a", dataset_id=s.dataset.id)
                for n, level in enumerate(lv for lv, k in kw["bloom_weights"].items() for _ in range(k))]

    async def dataset(*_a, **_k):
        return s.dataset.id

    async def notify(session, ids, title, body, data=None):
        sent.append((ids, title, data))

    monkeypatch.setattr("src.repositories.get_session", own_session)
    monkeypatch.setattr(homework_builder, "build_homework_questions", build)
    monkeypatch.setattr(homework_builder, "grounding_dataset_id", dataset)
    monkeypatch.setattr(push, "notify", notify)
    row = await service.own_quiz(db_session, quiz.id, s.student)
    await service.build_homework(row.id)

    assert asked["bloom_weights"] == {B.APPLY: 5} and asked["focus"].topic_id == s.ohm.id
    hw = await db_session.get(Homework, row.homework_id)
    assert hw.topic_id == s.ohm.id and hw.test_id is None and hw.total_questions == 5
    assert sent == [([s.student.id], "Homework ready: Ohm's law", {"type": "homework", "homeworkId": str(hw.id)})]
    loop = await _stage(db_session, s, s.ohm)
    assert loop.stage == "homework" and loop.homework_id == str(hw.id)
    await service.build_homework(row.id)  # once only
    assert len(sent) == 1

    # the re-check was built while the homework is going - still shut until it's done
    assert loop.recheck_id is not None
    with pytest.raises(ConflictError):
        await _start(db_session, s, s.ohm, attempt=2)
    hw_progress = (await db_session.execute(select(StudentHomeworkProgress).where(
        StudentHomeworkProgress.homework_id == hw.id))).scalar_one()
    hw_progress.status = StudentHomeworkStatus.COMPLETED
    await db_session.flush()
    assert (await _start(db_session, s, s.ohm, attempt=2)).id == loop.recheck_id  # opens at once, already built


async def test_checkpoints_are_built_when_a_topic_is_taught_and_only_then_announced(db_session, monkeypatch):
    import contextlib

    from src.services import push

    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm, just_now=True)
    await _pools(db_session, s.ohm)
    assert not [t for t in (await routes.get_my_topic_loops(None, None, user=s.student, session=db_session)).topics
                if t.topic_id == str(s.ohm.id)]  # not shown before its questions are ready

    class Proxy:  # the background task's own session -> the test's, commit = flush
        def __getattr__(self, name):
            return getattr(db_session, name)

        async def commit(self):
            await db_session.flush()

    @contextlib.asynccontextmanager
    async def own_session():
        yield Proxy()

    sent = []

    async def notify(session, ids, title, body, data=None):
        sent.append((list(ids), title, data))

    monkeypatch.setattr("src.repositories.get_session", own_session)
    monkeypatch.setattr(push, "notify", notify)
    await service.prebuild_class(s.cls.id, s.ohm.id, notify=True)

    loop = await _stage(db_session, s, s.ohm)
    assert loop.stage == "quiz" and loop.quiz_id is not None  # built, not started
    assert sent == [([s.student.id], "Your checkpoint is ready", {"type": "topic_quiz", "topicId": str(s.ohm.id)})]
    assert (await _start(db_session, s, s.ohm)).id == loop.quiz_id


async def test_each_question_is_on_its_own_bloom_level_generated_when_the_pool_has_none(db_session, monkeypatch):
    from types import SimpleNamespace

    from tests.test_daily_practice import _generation

    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    for q in await _pool(db_session, s.ohm, 6, bloom_level=B.REMEMBER):  # only Remember in the pool
        q.text += " r"
    calls = SimpleNamespace(generate=[], explain=[])
    _generation(monkeypatch, calls)
    quiz = await _start(db_session, s, s.ohm)
    # no spoken questions in the pool: those 2 slots become MCQs on their levels too
    assert [i.bloom_level for i in quiz.items] == [R, R, U, U, A, A, AN, AN, U, A]
    assert {i.kind for i in quiz.items} == {"mcq"}
    assert [slot["bloom_level"] for slot in calls.generate[0]["slots"]] == [U, U, A, A, AN, AN, U, A]

    # generation down: another level's question rather than none
    s2 = await _seed(db_session)
    await _teach(db_session, s2, s2.ohm)
    await _pool(db_session, s2.ohm, 8, bloom_level=B.REMEMBER)
    _generation(monkeypatch, calls, generate=False)
    quiz = await routes.start_my_topic_quiz(str(s2.ohm.id), QuizStartIn(attempt=1), user=s2.student,
                                            session=db_session)
    assert [i.bloom_level for i in quiz.items] == [R] * 8  # the pool's 8: nothing for the last 2 slots


async def test_a_spoken_answer_counts_at_its_level_and_feeds_the_analysis(db_session, monkeypatch):
    import base64

    from src.services import colearner_client, generation_client, practice_voice

    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    calls = []
    _stub_analysis(monkeypatch, calls)

    async def transcribe(*_a, **_k):
        return "current goes up I think"

    async def grade(**_k):
        return {"score": 30, "good": "You mention current.", "add": "Say how voltage relates to it."}

    monkeypatch.setattr(generation_client, "transcribe", transcribe)
    monkeypatch.setattr(colearner_client, "grade_answer", grade)
    quiz = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, quiz, {R, U, A, AN})
    spoken = next(i for i in quiz.items if i.kind == "voice" and i.bloom_level == U)
    await practice_voice.answer_voice_item(db_session, quiz.id, spoken.id, s.student,
                                           audio_base64=base64.b64encode(b"x" * 2000).decode(),
                                           mime_type="audio/mp4", token="t")
    a = (await _finish(db_session, s, quiz)).analysis  # the other spoken answer is skipped

    assert a.per_bloom[U] == 67 and a.lagging == [U] and a.outcome == "homework"  # 2 MCQs right, spoken 30
    [missed] = calls[0]["missed"]
    assert missed["question"].startswith("(answered aloud)") and missed["chosen"] == "current goes up I think"


async def test_chapter_focus_tells_milo_what_was_hard_and_what_is_mastered(db_session, monkeypatch):
    s = await _seed(db_session)
    assert await service.chapter_focus(db_session, s.student, s.electricity.id) is None  # no quiz yet
    for topic in (s.ohm, s.power, s.current):
        await _teach(db_session, s, topic)
        await _pools(db_session, topic)
    _stub_analysis(monkeypatch, [])
    ohm = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, ohm, {R, U})  # Apply and Analyze lag
    await _finish(db_session, s, ohm)
    power = await _start(db_session, s, s.power)
    await _answer(db_session, s, power, {R, U, A, AN})
    await _finish(db_session, s, power)
    await _start(db_session, s, s.current)  # opened, not finished: not mentioned

    focus = await service.chapter_focus(db_session, s.student, s.electricity.id)
    assert focus == "Found Apply and Analyze hard on Ohm's law. Has mastered: Power."
    assert await service.chapter_focus(db_session, s.student, s.light.id) is None


async def test_a_maths_quiz_runs_its_own_question_types_end_to_end(db_session, monkeypatch):
    from src.api.schemas.daily_practice import PracticeResponseIn
    from src.domain.models import Chapter, PracticeQuestion, Subject
    from tests.test_question_types import FIX, MINE, WHO

    s = await _seed(db_session)
    (await db_session.get(Subject, (await db_session.get(Chapter, s.ohm.chapter_id)).subject_id)).name = "Mathematics"
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    calls = []
    _stub_analysis(monkeypatch, calls)
    asked = []

    async def special_questions(payload):
        asked.append(payload)
        return {"questions": [
            {"slot_index": 0, "kind": "who_is_right", "bloom_level": "ANALYZE", "text": "Can 6ⁿ end in 0?", "spec": WHO},
            {"slot_index": 1, "kind": "fix_steps", "bloom_level": "EVALUATE", "text": FIX["problem"], "spec": FIX},
            {"slot_index": 2, "kind": "make_your_own", "bloom_level": "CREATE", "text": "HCF 6, LCM 36?",
             "spec": {**MINE, "example": ["6", "30"]}},  # its own example fails: dropped, becomes an MCQ
        ]}

    monkeypatch.setattr(service.daily_practice.generation_client, "special_questions", special_questions)
    quiz = await _start(db_session, s, s.ohm)
    assert [i.kind for i in quiz.items] == ["mcq"] * 6 + ["who_is_right", "fix_steps", "mcq"]
    assert [i.bloom_level for i in quiz.items][:6] == [R, U, A, A, A, A]  # mostly problems
    assert [sl["kind"] for sl in asked[0]["slots"]] == ["who_is_right", "fix_steps", "make_your_own"]
    assert "pair_hcf_lcm" in {c["kind"] for c in asked[0]["checks"]} and asked[0]["subject"] == "Mathematics"
    who = next(i for i in quiz.items if i.kind == "who_is_right")
    assert sorted(who.spec["reasons"]) == sorted(["6 is even", "6ⁿ has no factor 5", "Powers of 6 end in 6"])
    assert "correct" not in str(who.spec)  # never the answer before answering
    stored = (await db_session.execute(service.select(PracticeQuestion).where(
        PracticeQuestion.topic_id == s.ohm.id, PracticeQuestion.kind == "who_is_right"))).scalar_one()
    assert stored.origin == "generated"  # kept in the shared pool for the next student

    await _answer(db_session, s, quiz, {R, U, A, AN})  # the dropped make_your_own fell back to an Analyze MCQ
    fix = next(i for i in quiz.items if i.kind == "fix_steps")
    right_reason = next(i for i, r in enumerate(stored.spec["reasons"]) if r["correct"])  # shuffled when stored
    for item, response in ((who, {"pick": stored.spec["correct"], "reason": right_reason}), (fix, {"step": 0, "fix": 0})):
        out = await practice_routes.answer_my_practice_response(quiz.id, item.id, PracticeResponseIn(response=response),
                                                                 user=s.student, session=db_session)
        assert out.result.reveal["explanation"]
    assert out.result.score == 0 and out.result.message == "Milo's mistake is in step 3."
    again = await practice_routes.answer_my_practice_response(quiz.id, fix.id, PracticeResponseIn(
        response={"step": 2, "fix": 0}), user=s.student, session=db_session)
    assert again.result.score == 0  # the first answer counts

    a = (await _finish(db_session, s, quiz)).analysis
    assert a.per_bloom["ANALYZE"] == 100 and a.per_bloom["EVALUATE"] == 0 and "EVALUATE" in a.lagging
    assert list(a.per_bloom) == ["REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE", "EVALUATE"]  # Bloom order
    assert any(m["question"].startswith("(fix steps)") for m in calls[0]["missed"])
