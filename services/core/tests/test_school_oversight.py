"""School-wide oversight endpoints (GET /school/voice-tests, /homework,
/question-papers, /leaderboard) - real rows against the dev Postgres via
the db_session fixture, same pattern as test_subscription_limits.py. Two
things matter most here: no cross-school leakage, and pagination actually
works.
"""

import uuid

from src.api.routes import school_oversight
from src.domain.models import (
    AssignmentTargetMode,
    BloomLevel,
    Chapter,
    Dataset,
    Homework,
    HomeworkDifficulty,
    HomeworkQuestion,
    QuestionPaper,
    QuestionPaperQuestion,
    QuestionPaperStatus,
    QuestionPaperTopic,
    Role,
    School,
    SchoolClass,
    StudentPoints,
    StudentProfile,
    Subject,
    TestStatus,
    Topic,
    User,
    UserStatus,
    VoiceTest,
)


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_admin(db_session, school_id) -> User:
    admin = User(
        firebase_uid=f"admin-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Admin", role=Role.SCHOOL_ADMIN, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()
    return admin


async def _seed_class_subject_chapter(db_session, school_id) -> tuple[SchoolClass, Subject, Chapter]:
    school_class = SchoolClass(school_id=school_id, grade=8, section=uuid.uuid4().hex[:8].upper())
    subject = Subject(name=f"Subject-{uuid.uuid4()}")
    db_session.add_all([school_class, subject])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Chapter 1")
    db_session.add(chapter)
    await db_session.flush()
    return school_class, subject, chapter


async def _seed_voice_test(db_session, school_class, subject, chapter) -> VoiceTest:
    test = VoiceTest(
        class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, topic_id=None,
        duration_minutes=20, completion_window_hours=48,
        target_mode=AssignmentTargetMode.WHOLE_CLASS, status=TestStatus.DRAFT,
    )
    db_session.add(test)
    await db_session.flush()
    return test


async def test_school_voice_tests_has_no_cross_school_leakage(db_session):
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    admin_a = await _seed_admin(db_session, school_a.id)

    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school_a.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school_b.id)
    await _seed_voice_test(db_session, class_a, subject_a, chapter_a)
    await _seed_voice_test(db_session, class_b, subject_b, chapter_b)

    result = await school_oversight.list_school_voice_tests(
        class_id=None, teacher_id=None, limit=50, offset=0, user=admin_a, session=db_session
    )

    assert result.total == 1
    assert len(result.items) == 1
    assert result.items[0].subject_name == subject_a.name


async def test_school_voice_tests_pagination(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_admin(db_session, school.id)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    for _ in range(3):
        await _seed_voice_test(db_session, school_class, subject, chapter)

    first_page = await school_oversight.list_school_voice_tests(
        class_id=None, teacher_id=None, limit=2, offset=0, user=admin, session=db_session
    )
    second_page = await school_oversight.list_school_voice_tests(
        class_id=None, teacher_id=None, limit=2, offset=2, user=admin, session=db_session
    )

    assert first_page.total == 3
    assert len(first_page.items) == 2
    assert second_page.total == 3
    assert len(second_page.items) == 1


async def _seed_student(db_session, school_id, class_id, display_name) -> User:
    student = User(
        firebase_uid=f"student-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name=display_name, role=Role.STUDENT, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    db_session.add(StudentProfile(user_id=student.id, class_id=class_id, roll_number=1))
    await db_session.flush()
    return student


async def test_leaderboard_class_filter_ranks_only_within_that_class(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_admin(db_session, school.id)
    class_a, _, _ = await _seed_class_subject_chapter(db_session, school.id)
    class_b, _, _ = await _seed_class_subject_chapter(db_session, school.id)
    student_a = await _seed_student(db_session, school.id, class_a.id, "Student A")
    student_b = await _seed_student(db_session, school.id, class_b.id, "Student B")
    db_session.add_all([
        StudentPoints(student_id=student_a.id, points=10),
        StudentPoints(student_id=student_b.id, points=99),
    ])
    await db_session.flush()

    result = await school_oversight.get_school_leaderboard(
        class_id=str(class_a.id), limit=50, offset=0, user=admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].display_name == "Student A"
    assert result.items[0].rank == 1


async def test_question_bank_merges_paper_and_homework_sources_by_topic(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_admin(db_session, school.id)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    topic = Topic(chapter_id=chapter.id, name="Photosynthesis")
    db_session.add(topic)
    await db_session.flush()

    teacher = User(
        firebase_uid=f"t-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()

    paper = QuestionPaper(
        school_id=school.id, created_by=teacher.id, name="Unit Test", exam_type="UNIT_TEST",
        board="CBSE", grade=8, language="en", subject_id=subject.id, status=QuestionPaperStatus.FINALIZED,
    )
    db_session.add(paper)
    await db_session.flush()
    db_session.add_all([
        QuestionPaperTopic(paper_id=paper.id, topic_id=topic.id),
        QuestionPaperQuestion(paper_id=paper.id, order=1, bloom_level=BloomLevel.REMEMBER, marks=2, text="What is photosynthesis?"),
    ])

    test = await _seed_voice_test(db_session, school_class, subject, chapter)
    homework = Homework(
        test_id=test.id, class_id=school_class.id, gap_topic="Photosynthesis basics",
        gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
        target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(homework)
    dataset = Dataset(name="Bio Basics", question_count=10, description="Seed dataset")
    db_session.add(dataset)
    await db_session.flush()
    db_session.add(
        HomeworkQuestion(
            homework_id=homework.id, order=1, bloom_level=BloomLevel.UNDERSTAND,
            dataset_id=dataset.id, text="Explain the light reaction.", answer="It happens in the thylakoid.",
        )
    )
    await db_session.flush()

    result = await school_oversight.list_school_question_bank(
        source=None, topic=None, collection_name=None, general_bank_only=False, limit=50, offset=0, user=admin, session=db_session,
    )

    assert result.total == 2
    by_source = {item.source: item for item in result.items}
    assert by_source["QUESTION_PAPER"].topic_label == "Photosynthesis"
    assert by_source["QUESTION_PAPER"].answer is None
    assert by_source["HOMEWORK"].topic_label == "Photosynthesis basics"
    assert by_source["HOMEWORK"].answer == "It happens in the thylakoid."

    filtered = await school_oversight.list_school_question_bank(
        source=None, topic="basics", collection_name=None, general_bank_only=False, limit=50, offset=0, user=admin, session=db_session,
    )
    assert filtered.total == 1
    assert filtered.items[0].source == "HOMEWORK"

    case_insensitive = await school_oversight.list_school_question_bank(
        source=None, topic="PHOTOSYNTHESIS", collection_name=None, general_bank_only=False, limit=50, offset=0, user=admin, session=db_session,
    )
    assert case_insensitive.total == 2

    no_match = await school_oversight.list_school_question_bank(
        source=None, topic="nonexistent topic", collection_name=None, general_bank_only=False, limit=50, offset=0, user=admin, session=db_session,
    )
    assert no_match.total == 0


async def test_school_lists_search_every_page_and_show_the_chapter(db_session):
    school = await _seed_school(db_session)
    admin = await _seed_admin(db_session, school.id)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    for _ in range(3):
        await _seed_voice_test(db_session, school_class, subject, chapter)

    found = await school_oversight.list_school_voice_tests(
        class_id=None, teacher_id=None, q=chapter.name[:5].upper(), limit=1, offset=0, user=admin, session=db_session)
    assert found.total == 3 and found.items[0].chapter_name == chapter.name  # every page, not just the one shown
    none = await school_oversight.list_school_voice_tests(
        class_id=None, teacher_id=None, q="no-such-chapter", limit=50, offset=0, user=admin, session=db_session)
    assert none.total == 0


async def test_a_checkpoints_homework_and_a_maths_challenge_show_for_the_school(db_session):
    from src.domain.models import (AssignmentTargetMode, Homework, HomeworkDifficulty, HomeworkStatus, QuizTest,
                                   Topic)

    school = await _seed_school(db_session)
    admin = await _seed_admin(db_session, school.id)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    topic = Topic(chapter_id=chapter.id, name="Ohm's law")
    db_session.add(topic)
    await db_session.flush()
    db_session.add_all([
        Homework(test_id=None, topic_id=topic.id, class_id=school_class.id, gap_topic="Ohm's law",
                 gap_mastery_percent=40, total_questions=5, difficulty=HomeworkDifficulty.MIXED,
                 target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS, status=HomeworkStatus.ASSIGNED),
        QuizTest(class_id=school_class.id, subject_id=subject.id, chapter_id=chapter.id, title="Real Numbers",
                 status="ready", created_by=admin.id, topic_ids=[str(topic.id)], mix={"fix_steps": 5}),
    ])
    await db_session.flush()

    homework = await school_oversight.list_school_homework(
        class_id=None, q=None, limit=50, offset=0, user=admin, session=db_session)
    assert [(h.gap_topic, h.source) for h in homework.items] == [("Ohm's law", "Topic checkpoint")]
    challenges = await school_oversight.list_school_challenges(
        class_id=None, teacher_id=None, q="real", limit=50, offset=0, user=admin, session=db_session)
    assert [c.title for c in challenges.items] == ["Real Numbers"] and challenges.items[0].done_count == 0
