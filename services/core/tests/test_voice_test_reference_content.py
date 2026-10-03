"""Grounded reference-question generation for a Voice Test - split across
two things tested separately:

1. create_test schedules the generation as a FastAPI background task
   (via BackgroundTasks.add_task) rather than awaiting it inline - a real
   kg call reliably takes 10-25s, which previously made "Create" itself
   take that long and blow past nool-apps' 10s client request timeout
   (apiClient.ts's REQUEST_TIMEOUT_MS), so the app showed test creation as
   failed/timed out even though Core eventually succeeded. Verified here
   with a fake BackgroundTasks that just records what was scheduled,
   proving create_test returns without ever calling kg_client itself.

2. _generate_reference_content (the generation logic the background task
   ultimately runs) - kg_client.generate_assessor_content is monkeypatched
   here (a real call was already verified by hand against a running kg +
   Neo4j); this only tests Core's own wiring: chapters without a KG
   order_index are skipped, a successful call writes
   reference_questions/textbook_context, and a failed call doesn't raise.

Real rows against the dev Postgres via db_session, same pattern as
test_voice_test.py.
"""

import uuid

from src.api.routes import voice_test
from src.api.schemas.roster import AssignmentTarget
from src.api.schemas.voice_test import CreateTestIn
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Role,
    School,
    SchoolClass,
    SchoolCurriculum,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
    VoiceTest,
)


class FakeBackgroundTasks:
    """Records scheduled tasks instead of running them - proves
    create_test hands off generation rather than awaiting it inline.
    """

    def __init__(self):
        self.scheduled: list[tuple] = []

    def add_task(self, func, *args, **kwargs):
        self.scheduled.append((func, args, kwargs))


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email=f"{uuid.uuid4()}@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_class_subject_chapter_topic(db_session, school_id, *, order_index: int | None):
    school_class = SchoolClass(school_id=school_id, grade=10, section=str(uuid.uuid4())[:1].upper())
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=order_index)
    db_session.add(chapter)
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="Ohm's Law")
    db_session.add(topic)
    await db_session.flush()
    return school_class, subject, chapter, topic


async def _seed_teacher(db_session, school_id) -> User:
    teacher = User(
        firebase_uid=f"teacher-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    return teacher


def _body(school_class, subject, chapter, topic) -> CreateTestIn:
    return CreateTestIn(
        class_id=str(school_class.id), subject_id=str(subject.id), chapter_id=str(chapter.id),
        topic_id=str(topic.id), bloom_levels=[BloomLevel.UNDERSTAND, BloomLevel.APPLY], duration_minutes=20,
        completion_window_hours=48, target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS),
    )


async def test_create_test_schedules_generation_in_the_background_without_awaiting_it(db_session, monkeypatch):
    school = await _seed_school(db_session)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(
        db_session, school.id, order_index=11
    )
    teacher = await _seed_teacher(db_session, school.id)
    db_session.add(TeacherClassAssignment(teacher_id=teacher.id, class_id=school_class.id, subject_id=subject.id))
    # a Super Admin has given the school this subject
    db_session.add(SchoolCurriculum(school_id=school_class.school_id, subject_id=subject.id, enabled=True))
    await db_session.flush()

    called = False

    async def fake_generate(**kwargs):
        # If create_test awaited this inline (the bug being guarded
        # against), this would run synchronously during the call below.
        nonlocal called
        called = True
        return {"reference_questions": ["x"], "textbook_context": "y"}

    monkeypatch.setattr("src.api.routes.voice_test.kg_client.generate_assessor_content", fake_generate)

    background_tasks = FakeBackgroundTasks()
    result = await voice_test.create_test(
        _body(school_class, subject, chapter, topic),
        background_tasks=background_tasks,
        user=teacher,
        session=db_session,
    )

    assert result.id  # created fine, fast
    assert called is False  # generation was NOT run inline

    assert len(background_tasks.scheduled) == 1
    func, args, _kwargs = background_tasks.scheduled[0]
    assert func is voice_test._generate_reference_content_in_background
    assert args[0] == uuid.UUID(result.id)
    assert set(args[1]) == {"UNDERSTAND", "APPLY"}


async def test_skips_generation_when_chapter_has_no_kg_order_index(db_session, monkeypatch):
    school = await _seed_school(db_session)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(
        db_session, school.id, order_index=None
    )

    called = False

    async def fake_generate(**kwargs):
        nonlocal called
        called = True
        return {"reference_questions": ["x"], "textbook_context": "y"}

    monkeypatch.setattr("src.api.routes.voice_test.kg_client.generate_assessor_content", fake_generate)

    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=topic.id,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(test)
    await db_session.flush()

    await voice_test._generate_reference_content(db_session, test, ["UNDERSTAND", "APPLY"])

    assert called is False
    assert test.reference_questions is None


async def test_populates_reference_content_on_successful_generation(db_session, monkeypatch):
    school = await _seed_school(db_session)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(
        db_session, school.id, order_index=11
    )

    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return {"reference_questions": ["Why does X happen?"], "textbook_context": "V = IR..."}

    monkeypatch.setattr("src.api.routes.voice_test.kg_client.generate_assessor_content", fake_generate)

    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=topic.id,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(test)
    await db_session.flush()

    await voice_test._generate_reference_content(db_session, test, ["UNDERSTAND", "APPLY"])

    assert test.reference_questions == ["Why does X happen?"]
    assert test.textbook_context == "V = IR..."

    assert captured["board"] == "CBSE"
    assert captured["grade"] == 10
    assert captured["chapters"] == [{"number": 11, "name": "Electricity"}]
    assert captured["topic_names"] == ["Ohm's Law"]
    assert set(captured["bloom_levels"]) == {"UNDERSTAND", "APPLY"}


async def test_generation_failure_does_not_raise(db_session, monkeypatch):
    school = await _seed_school(db_session)
    school_class, subject, chapter, topic = await _seed_class_subject_chapter_topic(
        db_session, school.id, order_index=11
    )

    async def fake_generate(**kwargs):
        raise RuntimeError("kg service unreachable")

    monkeypatch.setattr("src.api.routes.voice_test.kg_client.generate_assessor_content", fake_generate)

    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=topic.id,
        duration_minutes=20, completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(test)
    await db_session.flush()

    await voice_test._generate_reference_content(db_session, test, ["UNDERSTAND", "APPLY"])

    assert test.reference_questions is None
    assert test.textbook_context is None
