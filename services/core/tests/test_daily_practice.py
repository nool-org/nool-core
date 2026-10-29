"""Daily practice: the planner (taught topics only, 6+2, earlier-chapter
review, no repeats within 14 days), the question pool (reuse, bank import,
generation, explanations written once), MCQ answers, mastery and XP math,
completion, and the teacher's progress toggle and class summary.

Real rows against the dev Postgres via db_session; the generation service
and practice_voice's voice prep are stubbed - no model calls."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import func, select

from shared.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from src.api.routes import daily_practice as routes
from src.api.schemas.daily_practice import PracticeMcqAnswerIn, TopicTaughtIn
from src.domain.models import (
    BloomLevel,
    Chapter,
    ClassTopicProgress,
    DailyPracticeItem,
    DailyPracticeSession,
    Dataset,
    DatasetQuestion,
    PracticeQuestion,
    QuestionType,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    SchoolDataset,
    StudentPoints,
    StudentProfile,
    StudentTopicMastery,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
)
from src.services import daily_practice as service
from src.services import practice_voice, push

B = BloomLevel


# ------------------------------------------------------------------ seed
def _user(role, school_id, name="U"):
    return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=name,
                role=role, school_id=school_id, status=UserStatus.ACTIVE)


async def _seed(db, students=1) -> SimpleNamespace:
    """Two chapters: 'Light' (earlier, 1 topic) and 'Electricity' (3 topics),
    a teacher of the class, students, and a visible bank dataset."""
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db.add_all([school, subject])
    await db.flush()
    section = SchoolClass(school_id=school.id, grade=10, section=uuid.uuid4().hex[:8].upper())
    light = Chapter(subject_id=subject.id, name="Light", order_index=1, grade=10)
    electricity = Chapter(subject_id=subject.id, name="Electricity", order_index=2, grade=10)
    teacher = _user(Role.TEACHER, school.id, "Ms T")
    kids = [_user(Role.STUDENT, school.id, f"Kid {n}") for n in range(students)]
    db.add_all([section, light, electricity, teacher, *kids])
    await db.flush()
    mirrors = Topic(chapter_id=light.id, name="Mirrors", text_content="A plane mirror ...")
    current, ohm, power = (Topic(chapter_id=electricity.id, name=n, text_content=f"{n} text ...")
                           for n in ("Current", "Ohm's law", "Power"))
    dataset = Dataset(name="Bank", question_count=0, description="", subject_id=subject.id, grade=10, board="CBSE")
    db.add_all([mirrors, current, ohm, power, dataset,
                TeacherClassAssignment(teacher_id=teacher.id, class_id=section.id, subject_id=subject.id),
                SchoolCurriculum(school_id=school.id, subject_id=subject.id, enabled=True),
                *(StudentProfile(user_id=k.id, class_id=section.id, roll_number=n) for n, k in enumerate(kids, 1))])
    await db.flush()
    db.add(SchoolDataset(school_id=school.id, dataset_id=dataset.id, enabled=True))
    await db.flush()
    return SimpleNamespace(school=school, cls=section, light=light, electricity=electricity, teacher=teacher,
                           student=kids[0] if kids else None, students=kids, mirrors=mirrors, current=current,
                           ohm=ohm, power=power, dataset=dataset)


async def _teach(db, s, topic, days_ago=0, just_now=False):
    """Marked taught - by default long enough ago that its checkpoint shows
    even unbuilt (topic_quiz.READY_GRACE); `just_now` for the moment itself."""
    marked = datetime.now(timezone.utc) - (timedelta(0) if just_now else timedelta(hours=1))
    db.add(ClassTopicProgress(class_id=s.cls.id, chapter_id=topic.chapter_id, topic_id=topic.id,
                              taught_on=service.today_ist() - timedelta(days=days_ago), created_at=marked))
    await db.flush()


async def _bank(db, s, topic, mcq=0, voice=0):
    for n in range(mcq):
        db.add(DatasetQuestion(dataset_id=s.dataset.id, chapter_id=topic.chapter_id, topic_id=topic.id,
                               bloom_level=[B.REMEMBER, B.UNDERSTAND, B.APPLY][n % 3], question_type=QuestionType.MCQ,
                               text=f"{topic.name} mcq {n}", options=["w1", "right", "w2", "w3"], answer="right",
                               created_by=s.teacher.id, external_uid=f"uid-{uuid.uuid4()}"))
    for n in range(voice):
        db.add(DatasetQuestion(dataset_id=s.dataset.id, chapter_id=topic.chapter_id, topic_id=topic.id,
                               bloom_level=B.UNDERSTAND, question_type=QuestionType.SHORT_ANSWER,
                               text=f"Explain {topic.name} {n}.", answer="Because ...", created_by=s.teacher.id,
                               external_uid=f"uid-{uuid.uuid4()}"))
    await db.flush()


async def _pool(db, topic, n, kind="mcq", bloom_level=B.UNDERSTAND, origin_override=None, **kw):
    rows = [PracticeQuestion(chapter_id=topic.chapter_id, topic_id=topic.id, bloom_level=bloom_level, kind=kind,
                             text=f"{topic.name} pool {kind} {i}",
                             options=["a", "b", "c", "d"] if kind == "mcq" else None,
                             correct_index=1 if kind == "mcq" else None, explanation="why" if kind == "mcq" else None,
                             hint="hint" if kind == "mcq" else None, expected_points=["p"] if kind == "voice" else [],
                             origin=origin_override or "bank", used_count=0, **kw)
            for i in range(n)]
    db.add_all(rows)
    await db.flush()
    return rows


async def _full_class(db, s):
    """All four topics taught (Light first), each with bank MCQs and short answers."""
    await _teach(db, s, s.mirrors, 30)
    await _teach(db, s, s.current, 5)
    await _teach(db, s, s.ohm, 3)
    await _teach(db, s, s.power, 1)
    for topic in (s.mirrors, s.current, s.ohm, s.power):
        await _bank(db, s, topic, mcq=6, voice=3)


@pytest.fixture(autouse=True)
def _no_models(monkeypatch):
    """No generation service unless a test stubs one; voice prep recorded."""
    calls = SimpleNamespace(voice_prep=[], generate=[], explain=[])
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: False)

    async def prepare(session, questions):
        calls.voice_prep.append(list(questions))
        for q in questions:
            q.expected_points = ["point"]

    monkeypatch.setattr(practice_voice, "prepare_voice_questions", prepare)
    return calls


def _generation(monkeypatch, calls, *, generate=True):
    monkeypatch.setattr(service.generation_client, "is_configured", lambda: True)

    async def generate_mcq(payload):
        calls.generate.append(payload)
        if not generate:
            return {"questions": []}
        return {"questions": [{"slot_index": i, "text": f"gen {s['concept']} {i}", "options": ["r", "x", "y", "z"],
                               "answer": "r", "explanation": "", "hint": ""}
                              for i, s in enumerate(payload["slots"])]}

    async def explain_mcq(payload):
        calls.explain.append(payload)
        return {"results": [{"question_number": i["question_number"], "explanation": f"because {i['text']}",
                             "hint": "think"} for i in payload["items"]]}

    monkeypatch.setattr(service.generation_client, "generate_mcq", generate_mcq)
    monkeypatch.setattr(service.generation_client, "explain_mcq", explain_mcq)


async def _today(db, student):
    return await routes.get_my_daily_practice(user=student, session=db)


async def _answer(db, student, out, item, option=1):
    return await routes.answer_my_daily_practice_mcq(out.session_id, item.id, PracticeMcqAnswerIn(option_index=option),
                                                     user=student, session=db)


async def _answer_all(db, s, out, right=True):
    for item in out.items:
        if item.kind == "mcq":
            q = await _question_of(db, item.id)
            await _answer(db, s.student, out, item, q.correct_index if right else (q.correct_index + 1) % 4)
        else:
            row = await db.get(DailyPracticeItem, uuid.UUID(item.id))
            row.transcript, row.answered_at = "my answer", datetime.now(timezone.utc)
            await service.record_item_result(db, row, score=80 if right else 20)


async def _question_of(db, item_id) -> PracticeQuestion:
    item = await db.get(DailyPracticeItem, uuid.UUID(item_id))
    return await db.get(PracticeQuestion, item.practice_question_id)


# ---------------------------------------------------------------- planner
def _taught(*rows):
    return [(SimpleNamespace(chapter_id=ch, taught_on=service.today_ist() - timedelta(days=d)),
             SimpleNamespace(id=uuid.uuid4(), name=name, chapter_id=ch)) for ch, name, d in rows]


def test_plan_is_6_mcq_and_2_voice_over_taught_topics_with_an_earlier_chapter_review():
    old, cur = uuid.uuid4(), uuid.uuid4()
    taught = _taught((cur, "newest", 0), (cur, "older", 4), (old, "weak earlier", 30), (old, "strong earlier", 31))
    now = datetime.now(timezone.utc)
    mastery = {taught[2][1].id: SimpleNamespace(mastery=30, due_at=now + timedelta(days=2)),
               taught[3][1].id: SimpleNamespace(mastery=95, due_at=now + timedelta(days=9))}
    slots = service.plan(taught, mastery, now)
    assert [s.kind for s in slots].count("mcq") == 6 and [s.kind for s in slots].count("voice") == 2
    assert {s.topic.id for s in slots} <= {t.id for _, t in taught}
    assert sum(s.topic.name == "newest" for s in slots) >= 3
    [review] = [s for s in slots if s.review]
    assert review.topic.name == "weak earlier" and review.kind == "mcq"
    assert all(s.topic.chapter_id == cur for s in slots if not s.review)
    assert {s.bloom_level for s in slots} <= {B.REMEMBER, B.UNDERSTAND, B.APPLY}  # nothing strong yet


def test_plan_without_earlier_chapters_reviews_the_current_one_and_adds_analyze_when_strong():
    cur = uuid.uuid4()
    taught = _taught((cur, "a", 0), (cur, "b", 2))
    now = datetime.now(timezone.utc)
    mastery = {taught[1][1].id: SimpleNamespace(mastery=90, due_at=now - timedelta(hours=1))}
    slots = service.plan(taught, mastery, now)
    assert len(slots) == 8 and not any(s.review for s in slots)
    assert [s.bloom_level for s in slots].count(B.ANALYZE) == 1
    assert next(s for s in slots if s.bloom_level == B.ANALYZE).topic.name == "b"
    assert sum(s.topic.name == "b" for s in slots) >= 2  # it's due


def test_mastery_due_and_xp_math():
    assert service.next_mastery(None, 40) == 40
    assert service.next_mastery(50, 100) == 65
    assert service.replay([100, 0, 0]) == 49 and service.replay([]) is None
    assert [service.interval_days(80, n) for n in range(6)] == [1, 3, 7, 14, 30, 30]
    assert service.interval_days(60, 4) == 1
    assert [service.voice_xp(s, "x") for s in (70, 40, 10)] == [10, 5, 2] and service.voice_xp(10, " ") == 0
    today = service.today_ist()
    days = {today - timedelta(days=n) for n in (1, 2, 3, 5)}
    assert service.streak_days(days, today) == 3 and service.streak_days(days | {today}, today) == 4
    assert service.streak_days({today - timedelta(days=2)}, today) == 0
    assert service.tidy("2.1 ACIDS, BASES AND SALTS") == "2.1 Acids, Bases and Salts"
    assert service.tidy("Ohm's law") == "Ohm's law"


# ------------------------------------------------------------- the session
async def test_nothing_taught_means_no_session(db_session):
    s = await _seed(db_session)
    out = await _today(db_session, s.student)
    assert out.status == "none" and out.empty_reason == "no_topics_taught" and out.session_id is None
    assert out.items == [] and out.date == service.today_ist()
    assert (await db_session.execute(select(func.count()).select_from(DailyPracticeSession).where(
        DailyPracticeSession.student_id == s.student.id))).scalar_one() == 0


async def test_no_questions_anywhere(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current)
    out = await _today(db_session, s.student)
    assert out.status == "none" and out.empty_reason == "no_questions" and out.session_id is None


async def test_builds_today_from_the_bank_once(db_session, _no_models):
    s = await _seed(db_session)
    await _full_class(db_session, s)
    out = await _today(db_session, s.student)
    assert out.status == "open" and out.empty_reason is None and out.chapter.name == "Electricity"
    assert len(out.items) == 8 and [i.order for i in out.items] == list(range(1, 9))
    assert [i.kind for i in out.items].count("voice") == 2
    taught_names = {"Mirrors", "Current", "Ohm's law", "Power"}
    assert {i.topic_name for i in out.items} <= taught_names
    # the earlier chapter's topic was never practised, so it isn't weak or due: no review item yet
    assert not any(i.review for i in out.items) and "Mirrors" not in {i.topic_name for i in out.items}
    assert all(not i.answered and i.result is None for i in out.items)
    assert all(len(i.options) == 4 for i in out.items if i.kind == "mcq")
    assert all(i.options is None for i in out.items if i.kind == "voice")
    pool = (await db_session.execute(select(PracticeQuestion).where(PracticeQuestion.topic_id.in_(
        [s.mirrors.id, s.current.id, s.ohm.id, s.power.id])))).scalars().all()
    assert pool and all(q.origin == "bank" and q.source_question_id for q in pool)
    assert _no_models.voice_prep and all(q.kind == "voice" for q in _no_models.voice_prep[0])
    # a second open is the same session, not a new one
    again = await _today(db_session, s.student)
    assert again.session_id == out.session_id and [i.id for i in again.items] == [i.id for i in out.items]


async def test_review_comes_from_a_weak_earlier_chapter_topic(db_session):
    s = await _seed(db_session)
    await _full_class(db_session, s)
    db_session.add(StudentTopicMastery(student_id=s.student.id, topic_id=s.mirrors.id, chapter_id=s.light.id,
                                       mastery=30, attempts=2, correct=0))
    await db_session.flush()
    out = await _today(db_session, s.student)
    [review] = [i for i in out.items if i.review]
    assert review.topic_name == "Mirrors"
    assert all(i.topic_name != "Mirrors" for i in out.items if not i.review)


async def test_no_question_repeats_within_14_days(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current)
    recent = await _pool(db_session, s.current, 8)
    fresh = await _pool(db_session, s.current, 8)
    old = await _pool(db_session, s.current, 8)
    await _pool(db_session, s.current, 2, kind="voice")
    for days_ago, rows in ((13, recent), (15, old)):
        past = DailyPracticeSession(student_id=s.student.id, chapter_id=s.electricity.id, status="completed",
                                    practice_date=service.today_ist() - timedelta(days=days_ago))
        db_session.add(past)
        await db_session.flush()
        db_session.add_all([DailyPracticeItem(session_id=past.id, order=n, practice_question_id=q.id, kind="mcq",
                                              topic_id=s.current.id, bloom_level=B.UNDERSTAND)
                            for n, q in enumerate(rows, 1)])
    for q in fresh:  # make the 15-day-old ones the least used, so only the 14-day rule keeps "recent" out
        q.used_count = 5
    await db_session.flush()
    out = await _today(db_session, s.student)
    used = {(await db_session.get(DailyPracticeItem, uuid.UUID(i.id))).practice_question_id for i in out.items}
    assert len(out.items) == 8 and not used & {q.id for q in recent}
    assert used & {q.id for q in old}


# ------------------------------------------------------------ question pool
async def test_real_bank_questions_come_before_written_ones(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current)
    written = await _pool(db_session, s.current, 6, origin_override="generated")
    await _bank(db_session, s, s.current, mcq=6)
    out = await _today(db_session, s.student)
    mcqs = [i.text for i in out.items if i.kind == "mcq"]
    # all 6 bank (board) questions are used; written ones only fill the 2 slots left
    assert sum(t.startswith("Current mcq") for t in mcqs) == 6 and len(mcqs) == 8
    assert sum(q.used_count for q in written) == 2


async def test_an_unspeakable_or_unreworded_spoken_question_is_never_served(db_session, monkeypatch):
    """C-002: a bank question that can't be answered aloud is never imported; a spoken one that
    couldn't be reworded is replaced by an MCQ on its topic, never served raw."""
    s = await _seed(db_session)
    await _full_class(db_session, s)
    blanks = DatasetQuestion(dataset_id=s.dataset.id, chapter_id=s.current.chapter_id, topic_id=s.current.id,
                             bloom_level=B.UNDERSTAND, question_type=QuestionType.SHORT_ANSWER,
                             text="Fill in the blanks: current is measured in ________.", answer="amperes",
                             created_by=s.teacher.id, external_uid=f"uid-{uuid.uuid4()}")
    db_session.add(blanks)
    await db_session.flush()

    async def prepare_fails(session, questions):
        return None  # generation couldn't reword any of them
    monkeypatch.setattr(practice_voice, "prepare_voice_questions", prepare_fails)
    out = await _today(db_session, s.student)
    assert len(out.items) == 8 and {i.kind for i in out.items} == {"mcq"}
    assert (await db_session.execute(select(PracticeQuestion.id).where(
        PracticeQuestion.source_question_id == blanks.id))).first() is None


async def test_generates_mcqs_when_pool_and_bank_are_empty(db_session, monkeypatch, _no_models):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current)
    _generation(monkeypatch, _no_models)
    out = await _today(db_session, s.student)
    # no speakable question anywhere -> the voice slots become MCQs too
    assert len(out.items) == 8 and {i.kind for i in out.items} == {"mcq"}
    [payload] = _no_models.generate
    assert len(payload["slots"]) == 8 and all(sl["concept"] == "Current" for sl in payload["slots"])
    assert payload["sections"] and payload["grade"] == 10
    rows = (await db_session.execute(select(PracticeQuestion).where(
        PracticeQuestion.topic_id == s.current.id))).scalars().all()
    assert len(rows) == 8 and all(q.origin == "generated" and q.options[q.correct_index] == "r" for q in rows)


async def test_explanations_are_written_once_per_batch_and_kept(db_session, monkeypatch, _no_models):
    s = await _seed(db_session, students=2)
    await _teach(db_session, s, s.current)
    await _bank(db_session, s, s.current, mcq=6, voice=2)
    _generation(monkeypatch, _no_models, generate=False)
    first = await _today(db_session, s.students[0])
    assert len(_no_models.explain) == 1 and len(_no_models.explain[0]["items"]) == 6
    q = await _question_of(db_session, first.items[0].id)
    assert q.explanation.startswith("because") and q.hint == "think"
    # the next student gets the same pool rows - already explained, nothing more is asked for
    second = await _today(db_session, s.students[1])
    assert len(second.items) == 8 and len(_no_models.explain) == 1
    assert not _no_models.generate


# ---------------------------------------------------------------- answers
async def test_mcq_answer_marks_first_answer_only_and_updates_mastery(db_session):
    s = await _seed(db_session, students=2)
    await _full_class(db_session, s)
    out = await _today(db_session, s.student)
    item = next(i for i in out.items if i.kind == "mcq")
    q = await _question_of(db_session, item.id)
    res = await _answer(db_session, s.student, out, item, q.correct_index)
    assert res.correct and res.correct_index == q.correct_index and res.xp_awarded == 5 and res.combo == 1
    assert res.explanation
    again = await _answer(db_session, s.student, out, item, (q.correct_index + 1) % 4)
    assert again.correct and again.xp_awarded == 5  # the stored first answer
    m = (await db_session.execute(select(StudentTopicMastery).where(
        StudentTopicMastery.student_id == s.student.id, StudentTopicMastery.topic_id == q.topic_id))).scalar_one()
    assert (m.mastery, m.attempts, m.correct) == (100, 1, 1)
    assert timedelta(hours=23) < m.due_at - datetime.now(timezone.utc) <= timedelta(days=1)
    points = await db_session.get(StudentPoints, s.student.id)
    assert points.points == 5
    shown = await _today(db_session, s.student)
    answered = next(i for i in shown.items if i.id == item.id)
    assert answered.answered and answered.result.correct and answered.result.correct_index == q.correct_index
    assert shown.xp_awarded == 5

    with pytest.raises(NotFoundError):  # someone else's session
        await _answer(db_session, s.students[1], out, item)
    with pytest.raises(NotFoundError):
        await routes.answer_my_daily_practice_mcq("nope", item.id, PracticeMcqAnswerIn(option_index=0),
                                                  user=s.student, session=db_session)
    voice = next(i for i in out.items if i.kind == "voice")
    with pytest.raises(ValidationError):
        await _answer(db_session, s.student, out, voice)


async def test_mastery_moves_and_due_spreads_out_with_good_answers(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current)
    await _pool(db_session, s.current, 8)
    await _pool(db_session, s.current, 2, kind="voice")
    out = await _today(db_session, s.student)
    mcqs = [i for i in out.items if i.kind == "mcq"]
    q = await _question_of(db_session, mcqs[0].id)
    await _answer(db_session, s.student, out, mcqs[0], (q.correct_index + 1) % 4)  # wrong: 0
    await _answer(db_session, s.student, out, mcqs[1], (await _question_of(db_session, mcqs[1].id)).correct_index)
    await _answer(db_session, s.student, out, mcqs[2], (await _question_of(db_session, mcqs[2].id)).correct_index)
    m = (await db_session.execute(select(StudentTopicMastery).where(
        StudentTopicMastery.student_id == s.student.id, StudentTopicMastery.topic_id == s.current.id))).scalar_one()
    assert m.mastery == round(0.7 * 30 + 30) and (m.attempts, m.correct) == (3, 2)  # 0 -> 30 -> 51
    # the 2nd good answer in a row: next review in 3 days
    assert timedelta(days=2, hours=23) < m.due_at - datetime.now(timezone.utc) <= timedelta(days=3)


async def test_combo_bonus_from_the_third_correct_mcq_in_a_row(db_session):
    s = await _seed(db_session)
    await _full_class(db_session, s)
    out = await _today(db_session, s.student)
    mcqs = [i for i in out.items if i.kind == "mcq"]
    results = []
    for item in mcqs[:4]:
        results.append(await _answer(db_session, s.student, out, item,
                                     (await _question_of(db_session, item.id)).correct_index))
    assert [r.combo for r in results] == [1, 2, 3, 4]
    assert [r.xp_awarded for r in results] == [5, 5, 7, 7]
    wrong = await _answer(db_session, s.student, out, mcqs[4],
                          ((await _question_of(db_session, mcqs[4].id)).correct_index + 1) % 4)
    assert not wrong.correct and wrong.combo == 0 and wrong.xp_awarded == 0


# -------------------------------------------------------------- completion
async def test_complete_needs_every_answer_and_is_idempotent(db_session):
    s = await _seed(db_session)
    await _full_class(db_session, s)
    out = await _today(db_session, s.student)
    with pytest.raises(ConflictError):
        await routes.complete_my_daily_practice(out.session_id, user=s.student, session=db_session)
    await _answer_all(db_session, s, out)
    points_before = (await db_session.get(StudentPoints, s.student.id)).points
    done = await routes.complete_my_daily_practice(out.session_id, user=s.student, session=db_session)
    assert done.xp_awarded == 10 and done.streak_days == 1
    assert {m.topic_name for m in done.mastery} == {i.topic_name for i in out.items}
    assert all(m.before == 0 and m.after >= 70 for m in done.mastery)
    again = await routes.complete_my_daily_practice(out.session_id, user=s.student, session=db_session)
    assert again == done
    assert (await db_session.get(StudentPoints, s.student.id)).points == points_before + 10
    shown = await _today(db_session, s.student)
    assert shown.status == "completed" and shown.streak_days == 1 and all(i.answered for i in shown.items)
    voice = next(i for i in shown.items if i.kind == "voice")
    assert voice.result.score == 80 and voice.result.transcript == "my answer"
    wire = shown.model_dump(by_alias=True, mode="json")
    assert {"sessionId", "date", "chapter", "status", "items", "xpAwarded", "streakDays", "emptyReason",
            "newItems", "newTopics"} == set(wire)
    mcq_wire = next(i for i in wire["items"] if i["kind"] == "mcq")
    assert set(mcq_wire["result"]) == {"correct", "correctIndex", "explanation"} and "topicName" in mcq_wire
    assert set(next(i for i in wire["items"] if i["kind"] == "voice")["result"]) == {"transcript", "score", "feedback"}
    # 6 x 5 + combo 4 x 2 + 2 voice x 10 + 10 bonus
    assert shown.xp_awarded == 30 + 8 + 20 + 10
    with pytest.raises(ConflictError):  # no more answers once completed
        await _answer(db_session, s.student, out, out.items[0])


# ----------------------------------------------------------------- teacher
async def test_teacher_marks_topics_taught_and_sees_chapter_progress(db_session):
    s = await _seed(db_session, students=0)
    cid, chid = str(s.cls.id), str(s.electricity.id)
    marked = await routes.set_topic_taught(cid, str(s.ohm.id), TopicTaughtIn(taught=True), background_tasks=BackgroundTasks(), user=s.teacher,
                                           session=db_session)
    assert marked.taught_on == service.today_ist() and marked.name == "Ohm's law" and marked.order == 2
    day = service.today_ist() - timedelta(days=4)
    await routes.set_topic_taught(cid, str(s.current.id), TopicTaughtIn(taught=True, taught_on=day), background_tasks=BackgroundTasks(), user=s.teacher,
                                  session=db_session)
    progress = await routes.get_chapter_progress(cid, chid, user=s.teacher, session=db_session)
    assert progress.chapter.name == "Electricity"
    assert [(t.name, t.order, t.taught_on) for t in progress.topics] == [
        ("Current", 1, day), ("Ohm's law", 2, service.today_ist()), ("Power", 3, None)]
    assert (await service.current_chapter(db_session, s.cls.id)).id == s.electricity.id
    unmarked = await routes.set_topic_taught(cid, str(s.ohm.id), TopicTaughtIn(taught=False), background_tasks=BackgroundTasks(), user=s.teacher,
                                             session=db_session)
    assert unmarked.taught_on is None
    progress = await routes.get_chapter_progress(cid, chid, user=s.teacher, session=db_session)
    assert [t.taught_on for t in progress.topics] == [day, None, None]

    stranger = _user(Role.TEACHER, s.school.id)
    db_session.add(stranger)
    await db_session.flush()
    with pytest.raises(ForbiddenError):
        await routes.get_chapter_progress(cid, chid, user=stranger, session=db_session)
    with pytest.raises(ForbiddenError):
        await routes.set_topic_taught(cid, str(s.ohm.id), TopicTaughtIn(taught=True), background_tasks=BackgroundTasks(), user=stranger,
                                      session=db_session)
    with pytest.raises(ForbiddenError):
        await routes.get_daily_practice_summary(cid, user=stranger, session=db_session)
    with pytest.raises(NotFoundError):
        await routes.set_topic_taught(cid, "not-a-topic", TopicTaughtIn(taught=True), background_tasks=BackgroundTasks(), user=s.teacher,
                                      session=db_session)


async def test_class_summary(db_session):
    s = await _seed(db_session, students=3)
    asha, ben, cara = s.students
    await _full_class(db_session, s)
    today = service.today_ist()

    # Asha practises today, and practised yesterday
    s.student = asha
    out = await _today(db_session, asha)
    await _answer_all(db_session, s, out, right=False)
    await routes.complete_my_daily_practice(out.session_id, user=asha, session=db_session)
    db_session.add(DailyPracticeSession(student_id=asha.id, practice_date=today - timedelta(days=1),
                                        chapter_id=s.electricity.id, status="completed"))
    # Cara was strong on Power 10 days ago, weak on it 2 days ago; practised then, not since
    pool = await _pool(db_session, s.power, 2)
    for days_ago, score, q in ((10, 100, pool[0]), (2, 0, pool[1])):
        past = DailyPracticeSession(student_id=cara.id, practice_date=today - timedelta(days=days_ago),
                                    chapter_id=s.electricity.id, status="completed")
        db_session.add(past)
        await db_session.flush()
        db_session.add(DailyPracticeItem(session_id=past.id, order=1, practice_question_id=q.id, kind="mcq",
                                         topic_id=s.power.id, bloom_level=B.UNDERSTAND, score=score,
                                         correct=score >= 70,
                                         answered_at=datetime.now(timezone.utc) - timedelta(days=days_ago)))
    await db_session.flush()

    summary = await routes.get_daily_practice_summary(str(s.cls.id), user=s.teacher, session=db_session)
    assert summary.date == today and summary.current_chapter.name == "Electricity"
    assert (summary.student_count, summary.practised_count) == (3, 1)
    by_name = {st.name: st for st in summary.students}
    assert by_name["Kid 0"].practised_today and by_name["Kid 0"].streak_days == 2
    assert by_name["Kid 0"].last_practised_on == today
    assert not by_name["Kid 1"].practised_today and by_name["Kid 1"].last_practised_on is None
    assert (by_name["Kid 1"].answered_today, by_name["Kid 1"].items_today) == (0, 0)  # never opened today
    assert by_name["Kid 2"].last_practised_on == today - timedelta(days=2) and by_name["Kid 2"].streak_days == 0
    # Asha got everything wrong: every current-chapter topic she practised is 100% struggling
    assert summary.weakest_topics and all(w.struggling_percent == 100 for w in summary.weakest_topics)
    assert len(summary.weakest_topics) <= 3
    assert {w.topic_name for w in summary.weakest_topics} <= {"Current", "Ohm's law", "Power"}
    slipping = {x.name: x.reason for x in summary.slipping}
    assert slipping["Kid 1"] == "Hasn't started daily practice"
    assert "Power mastery dropped" in slipping["Kid 2"]  # 100 -> 70: practised within the last 3 days
    assert "Kid 0" not in slipping

    # a past date: nobody had practised yet
    earlier = await routes.get_daily_practice_summary(str(s.cls.id), date=today - timedelta(days=20),
                                                      user=s.teacher, session=db_session)
    assert earlier.practised_count == 0 and earlier.slipping == []


# ------------------------------------------------- topics taught later today
async def _backdate(db, out, hours=3):
    """The set was built earlier today (in a test everything shares one transaction's now())."""
    practice = await db.get(DailyPracticeSession, uuid.UUID(out.session_id))
    practice.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    await db.flush()


async def test_an_unstarted_set_is_rebuilt_when_a_topic_is_taught_later(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current, 1)
    for topic in (s.current, s.ohm):
        await _bank(db_session, s, topic, mcq=6, voice=3)
    first = await _today(db_session, s.student)
    assert {i.topic_name for i in first.items} == {"Current"}
    await _backdate(db_session, first)
    await _teach(db_session, s, s.ohm)
    rebuilt = await _today(db_session, s.student)
    assert rebuilt.session_id != first.session_id and "Ohm's law" in {i.topic_name for i in rebuilt.items}
    assert len(rebuilt.items) == 8 and rebuilt.new_items == 0 and rebuilt.new_topics == []
    assert await db_session.get(DailyPracticeSession, uuid.UUID(first.session_id)) is None
    again = await _today(db_session, s.student)
    assert again.session_id == rebuilt.session_id


async def test_a_completed_set_is_extended_and_reopened_without_a_second_bonus(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current, 1)
    await _bank(db_session, s, s.current, mcq=10, voice=3)
    for topic in (s.ohm, s.power):
        await _bank(db_session, s, topic, mcq=6)
    out = await _today(db_session, s.student)
    await _answer_all(db_session, s, out)
    await routes.complete_my_daily_practice(out.session_id, user=s.student, session=db_session)
    practice = await db_session.get(DailyPracticeSession, uuid.UUID(out.session_id))
    completed_at = practice.completed_at
    await _backdate(db_session, out)
    unchanged = await _today(db_session, s.student)  # nothing new taught: no extension
    assert unchanged.status == "completed" and len(unchanged.items) == 8 and unchanged.new_items == 0

    await _teach(db_session, s, s.ohm)
    await _teach(db_session, s, s.power)
    extended = await _today(db_session, s.student)
    assert extended.session_id == out.session_id and extended.status == "open" and extended.streak_days == 1
    new = extended.items[8:]
    assert len(extended.items) == 12 and [i.order for i in new] == [101, 102, 103, 104]
    assert all(i.kind == "mcq" and not i.review and not i.answered for i in new)
    assert sorted(i.topic_name for i in new) == ["Ohm's law", "Ohm's law", "Power", "Power"]
    assert extended.new_items == 4 and sorted(extended.new_topics) == ["Ohm's law", "Power"]
    await _answer(db_session, s.student, extended, new[0], (await _question_of(db_session, new[0].id)).correct_index)
    assert (await _today(db_session, s.student)).new_items == 3

    for item in new[1:]:
        await _answer(db_session, s.student, extended, item, (await _question_of(db_session, item.id)).correct_index)
    points = (await db_session.get(StudentPoints, s.student.id)).points
    done = await routes.complete_my_daily_practice(out.session_id, user=s.student, session=db_session)
    assert done.xp_awarded == 10 and {"Ohm's law", "Power"} <= {m.topic_name for m in done.mastery}
    assert (await db_session.get(StudentPoints, s.student.id)).points == points  # the bonus was paid once
    assert practice.status == "completed" and practice.completed_at == completed_at
    final = await _today(db_session, s.student)
    assert final.status == "completed" and final.new_items == 0 and len(final.items) == 12


async def test_at_most_two_extensions_and_16_items_a_day(db_session):
    s = await _seed(db_session)
    await _teach(db_session, s, s.current, 1)
    await _bank(db_session, s, s.current, mcq=8, voice=3)
    out = await _today(db_session, s.student)
    first = next(i for i in out.items if i.kind == "mcq")
    await _answer(db_session, s.student, out, first, 0)
    await _backdate(db_session, out)
    extra = [Topic(chapter_id=s.electricity.id, name=f"Extra {n}", text_content="...") for n in range(7)]
    db_session.add_all(extra)
    await db_session.flush()
    for topic in extra[:6]:
        await _pool(db_session, topic, 2)
        await _teach(db_session, s, topic)
    one = await _today(db_session, s.student)
    assert len(one.items) == 12 and one.new_items == 4 and len(one.new_topics) == 4
    two = await _today(db_session, s.student)  # the 2 topics the first extension had no room for
    assert len(two.items) == 16 and [i.order for i in two.items[12:]] == [201, 202, 203, 204]
    assert two.new_items == 4 and len(two.new_topics) == 2
    await _pool(db_session, extra[6], 2)
    await _teach(db_session, s, extra[6])
    three = await _today(db_session, s.student)
    assert len(three.items) == 16 and "Extra 6" not in {i.topic_name for i in three.items}


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake(session, user_ids, title, body, data=None):
        calls.append({"user_ids": {u for u in user_ids}, "title": title, "body": body, "data": data})
        return 1

    monkeypatch.setattr(push, "send_push", fake)
    return calls


async def test_marking_a_topic_taught_today_notifies_the_class_once_per_lesson(db_session, sent):
    """The notice itself waits for the checkpoints (topic_quiz.prebuild_class) - here, whether one is due."""
    s = await _seed(db_session, students=2)
    cid = str(s.cls.id)

    async def mark(topic, **kw):
        tasks = BackgroundTasks()
        await routes.set_topic_taught(cid, str(topic.id), TopicTaughtIn(taught=True, **kw), background_tasks=tasks,
                                      user=s.teacher, session=db_session)
        return next(t.args[2] for t in tasks.tasks if t.func.__name__ == "prebuild_class")

    assert await mark(s.current, taught_on=service.today_ist() - timedelta(days=1)) is False  # not a lesson today
    for row in (await db_session.execute(select(ClassTopicProgress).where(
            ClassTopicProgress.class_id == s.cls.id))).scalars().all():
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.flush()
    assert await mark(s.ohm) is True
    assert await mark(s.power) is False  # Ohm's law was marked within 30 minutes
    assert sent == []  # nothing before the checkpoints are ready


async def test_marking_a_topic_taught_queues_its_revision_cards_and_checkpoints(db_session):
    s = await _seed(db_session)
    tasks = BackgroundTasks()
    await routes.set_topic_taught(str(s.cls.id), str(s.ohm.id), TopicTaughtIn(taught=True),
                                  background_tasks=tasks, user=s.teacher, session=db_session)
    assert [(t.func.__name__, t.args) for t in tasks.tasks] == [
        ("pregenerate_for_topic", (s.ohm.id,)), ("prebuild_class", (s.cls.id, s.ohm.id, True))]


async def test_a_switched_off_bank_retires_what_the_pool_made_from_it(db_session):
    from sqlalchemy import update

    from src.domain.models import SchoolDataset

    s = await _seed(db_session)
    await _bank(db_session, s, s.mirrors, mcq=1)
    source = (await db_session.execute(select(DatasetQuestion).where(DatasetQuestion.topic_id == s.mirrors.id))
              ).scalars().first()
    [from_bank] = await _pool(db_session, s.mirrors, 1, source_question_id=source.id)
    [written] = await _pool(db_session, s.mirrors, 1, origin_override="generated")
    pick = lambda exclude: service._from_pool(db_session, s.mirrors, "mcq", B.UNDERSTAND, exclude)  # noqa: E731
    assert (await pick(set())).id == from_bank.id  # bank first while it's on

    await db_session.execute(update(SchoolDataset).where(SchoolDataset.dataset_id == s.dataset.id).values(enabled=False))
    assert (await pick(set())).id == written.id  # off: what came from it is skipped
    assert await pick({written.id}) is None


def test_near_identical_stems_are_one_question_but_other_topics_are_not():
    # C-012: three silver-chloride-in-sunlight stems in one topic quiz
    first = "What happens when white silver chloride is placed in a china dish and left in sunlight for some time?"
    again = "What happens to white silver chloride when it is kept in a china dish under sunlight for some time?"
    seen = [service.words_of(first)]
    assert service.near_duplicate(again, seen)
    assert not service.near_duplicate("Balance the equation for the burning of magnesium ribbon in air.", seen)
    assert not service.near_duplicate("Which gas is released when zinc reacts with dilute sulphuric acid?", seen)
    assert not service.near_duplicate("Define a combination reaction.", [])


async def test_a_generated_question_worded_like_a_pool_pick_is_not_added(db_session, monkeypatch):
    """C-016: only pool picks were checked, so generated fill-ins repeated the same idea."""
    s = await _seed(db_session)
    [first] = await _pool(db_session, s.mirrors, 1)
    first.text = "What happens when white silver chloride is placed in a china dish and left in sunlight for some time?"
    [twin] = await _pool(db_session, s.mirrors, 1, origin_override="generated")
    twin.text = "What happens to white silver chloride when it is kept in a china dish under sunlight for some time?"
    await db_session.flush()

    async def generate(session, school_class, chapter_id, slots, seed):
        return {n: twin for n in range(len(slots))}

    monkeypatch.setattr(service, "_generate", generate)
    slots = [service.Slot(s.mirrors, "mcq", B.UNDERSTAND), service.Slot(s.mirrors, "mcq", B.UNDERSTAND)]
    real = service._from_pool

    async def only_first(session, topic, kind, level, exclude, *a):
        return None if twin.id in exclude else await real(session, topic, kind, level, exclude, *a)

    monkeypatch.setattr(service, "_from_pool", only_first)
    chosen = await service._fill(db_session, s.student, s.cls, slots, service.today_ist(), "seed")
    assert [q.id for _, q in chosen] == [first.id]
