"""Super Admin's cross-tenant, read-only school oversight views
(src/api/routes/admin.py's `_as_admin` mirrors of school_oversight.py) -
real rows against the dev Postgres via db_session, same seed/assert shapes
as test_school_oversight.py but calling the new `_as_admin` functions with
an explicit school_id instead of relying on user.school_id. Covers, for
each route: a basic list-returns-expected-shape test, a filter test for
every filterable query param (this is what would catch a dropped `alias=`
on a Query param), and cross-school isolation.
"""

import uuid

from src.api.routes import admin
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


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


async def _seed_school(db_session) -> School:
    school = School(name=f"School-{uuid.uuid4()}", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_class_subject_chapter(db_session, school_id) -> tuple[SchoolClass, Subject, Chapter]:
    school_class = SchoolClass(school_id=school_id, grade=8, section=str(uuid.uuid4())[:8])
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


async def _seed_teacher(db_session, school_id) -> User:
    teacher = User(
        firebase_uid=f"teacher-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name=f"Teacher-{uuid.uuid4()}", role=Role.TEACHER, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    return teacher


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


# --- voice-tests ------------------------------------------------------------


async def test_list_school_voice_tests_as_admin_basic_shape(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    await _seed_voice_test(db_session, school_class, subject, chapter)

    result = await admin.list_school_voice_tests_as_admin(
        str(school.id), class_id=None, teacher_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].subject_name == subject.name


async def test_list_school_voice_tests_as_admin_class_id_filters(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school.id)
    await _seed_voice_test(db_session, class_a, subject_a, chapter_a)
    await _seed_voice_test(db_session, class_b, subject_b, chapter_b)

    result = await admin.list_school_voice_tests_as_admin(
        str(school.id), class_id=str(class_a.id), teacher_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].subject_name == subject_a.name


async def test_list_school_voice_tests_as_admin_teacher_id_filters(db_session):
    from src.domain.models import TeacherClassAssignment

    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school.id)
    teacher_a = await _seed_teacher(db_session, school.id)
    db_session.add(TeacherClassAssignment(teacher_id=teacher_a.id, class_id=class_a.id, subject_id=subject_a.id))
    await db_session.flush()
    await _seed_voice_test(db_session, class_a, subject_a, chapter_a)
    await _seed_voice_test(db_session, class_b, subject_b, chapter_b)

    result = await admin.list_school_voice_tests_as_admin(
        str(school.id), class_id=None, teacher_id=str(teacher_a.id), limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].subject_name == subject_a.name
    assert result.items[0].teacher_name == teacher_a.display_name


async def test_list_school_voice_tests_as_admin_no_cross_school_leakage(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school_a.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school_b.id)
    await _seed_voice_test(db_session, class_a, subject_a, chapter_a)
    await _seed_voice_test(db_session, class_b, subject_b, chapter_b)

    result = await admin.list_school_voice_tests_as_admin(
        str(school_a.id), class_id=None, teacher_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].subject_name == subject_a.name


# --- homework ---------------------------------------------------------------


async def test_list_school_homework_as_admin_basic_shape(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    test = await _seed_voice_test(db_session, school_class, subject, chapter)
    homework = Homework(
        test_id=test.id, class_id=school_class.id, gap_topic="Gap topic",
        gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
        target_mode=AssignmentTargetMode.WHOLE_CLASS,
    )
    db_session.add(homework)
    await db_session.flush()

    result = await admin.list_school_homework_as_admin(
        str(school.id), class_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].gap_topic == "Gap topic"


async def test_list_school_homework_as_admin_class_id_filters(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school.id)
    test_a = await _seed_voice_test(db_session, class_a, subject_a, chapter_a)
    test_b = await _seed_voice_test(db_session, class_b, subject_b, chapter_b)
    db_session.add_all([
        Homework(
            test_id=test_a.id, class_id=class_a.id, gap_topic="A topic",
            gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
            target_mode=AssignmentTargetMode.WHOLE_CLASS,
        ),
        Homework(
            test_id=test_b.id, class_id=class_b.id, gap_topic="B topic",
            gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
            target_mode=AssignmentTargetMode.WHOLE_CLASS,
        ),
    ])
    await db_session.flush()

    result = await admin.list_school_homework_as_admin(
        str(school.id), class_id=str(class_a.id), limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].gap_topic == "A topic"


async def test_list_school_homework_as_admin_no_cross_school_leakage(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school_a.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school_b.id)
    test_a = await _seed_voice_test(db_session, class_a, subject_a, chapter_a)
    test_b = await _seed_voice_test(db_session, class_b, subject_b, chapter_b)
    db_session.add_all([
        Homework(
            test_id=test_a.id, class_id=class_a.id, gap_topic="A topic",
            gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
            target_mode=AssignmentTargetMode.WHOLE_CLASS,
        ),
        Homework(
            test_id=test_b.id, class_id=class_b.id, gap_topic="B topic",
            gap_mastery_percent=40, total_questions=1, difficulty=HomeworkDifficulty.EASY,
            target_mode=AssignmentTargetMode.WHOLE_CLASS,
        ),
    ])
    await db_session.flush()

    result = await admin.list_school_homework_as_admin(
        str(school_a.id), class_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].gap_topic == "A topic"


# --- question-papers ---------------------------------------------------------


async def _seed_question_paper(db_session, school_id, subject, creator, *, name="Unit Test") -> QuestionPaper:
    paper = QuestionPaper(
        school_id=school_id, created_by=creator.id, name=name, exam_type="UNIT_TEST",
        board="CBSE", grade=8, language="en", subject_id=subject.id, status=QuestionPaperStatus.FINALIZED,
    )
    db_session.add(paper)
    await db_session.flush()
    return paper


async def test_list_school_question_papers_as_admin_basic_shape(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    _class, subject, _chapter = await _seed_class_subject_chapter(db_session, school.id)
    teacher = await _seed_teacher(db_session, school.id)
    paper = await _seed_question_paper(db_session, school.id, subject, teacher)

    result = await admin.list_school_question_papers_as_admin(
        str(school.id), subject_id=None, created_by=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].id == str(paper.id)
    assert result.items[0].created_by_name == teacher.display_name


async def test_list_school_question_papers_as_admin_subject_id_filters(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    _class, subject_a, _chapter = await _seed_class_subject_chapter(db_session, school.id)
    _class_b, subject_b, _chapter_b = await _seed_class_subject_chapter(db_session, school.id)
    teacher = await _seed_teacher(db_session, school.id)
    await _seed_question_paper(db_session, school.id, subject_a, teacher, name="Paper A")
    await _seed_question_paper(db_session, school.id, subject_b, teacher, name="Paper B")

    result = await admin.list_school_question_papers_as_admin(
        str(school.id), subject_id=str(subject_a.id), created_by=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].name == "Paper A"


async def test_list_school_question_papers_as_admin_created_by_filters(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    _class, subject, _chapter = await _seed_class_subject_chapter(db_session, school.id)
    teacher_a = await _seed_teacher(db_session, school.id)
    teacher_b = await _seed_teacher(db_session, school.id)
    await _seed_question_paper(db_session, school.id, subject, teacher_a, name="Paper A")
    await _seed_question_paper(db_session, school.id, subject, teacher_b, name="Paper B")

    result = await admin.list_school_question_papers_as_admin(
        str(school.id), subject_id=None, created_by=str(teacher_a.id), limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].name == "Paper A"


async def test_list_school_question_papers_as_admin_no_cross_school_leakage(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    _class_a, subject_a, _chapter_a = await _seed_class_subject_chapter(db_session, school_a.id)
    _class_b, subject_b, _chapter_b = await _seed_class_subject_chapter(db_session, school_b.id)
    teacher_a = await _seed_teacher(db_session, school_a.id)
    teacher_b = await _seed_teacher(db_session, school_b.id)
    await _seed_question_paper(db_session, school_a.id, subject_a, teacher_a, name="Paper A")
    await _seed_question_paper(db_session, school_b.id, subject_b, teacher_b, name="Paper B")

    result = await admin.list_school_question_papers_as_admin(
        str(school_a.id), subject_id=None, created_by=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].name == "Paper A"


# --- leaderboard --------------------------------------------------------------


async def test_get_school_leaderboard_as_admin_basic_shape(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class, _subject, _chapter = await _seed_class_subject_chapter(db_session, school.id)
    student = await _seed_student(db_session, school.id, school_class.id, "Student 1")
    db_session.add(StudentPoints(student_id=student.id, points=42))
    await db_session.flush()

    result = await admin.get_school_leaderboard_as_admin(
        str(school.id), class_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].points == 42
    assert result.items[0].rank == 1


async def test_get_school_leaderboard_as_admin_class_id_ranks_only_within_that_class(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    class_a, _, _ = await _seed_class_subject_chapter(db_session, school.id)
    class_b, _, _ = await _seed_class_subject_chapter(db_session, school.id)
    student_a = await _seed_student(db_session, school.id, class_a.id, "Student A")
    student_b = await _seed_student(db_session, school.id, class_b.id, "Student B")
    db_session.add_all([
        StudentPoints(student_id=student_a.id, points=10),
        StudentPoints(student_id=student_b.id, points=99),
    ])
    await db_session.flush()

    result = await admin.get_school_leaderboard_as_admin(
        str(school.id), class_id=str(class_a.id), limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].display_name == "Student A"
    assert result.items[0].rank == 1


async def test_get_school_leaderboard_as_admin_no_cross_school_leakage(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    class_a, _, _ = await _seed_class_subject_chapter(db_session, school_a.id)
    class_b, _, _ = await _seed_class_subject_chapter(db_session, school_b.id)
    student_a = await _seed_student(db_session, school_a.id, class_a.id, "Student A")
    student_b = await _seed_student(db_session, school_b.id, class_b.id, "Student B")
    db_session.add_all([
        StudentPoints(student_id=student_a.id, points=10),
        StudentPoints(student_id=student_b.id, points=99),
    ])
    await db_session.flush()

    result = await admin.get_school_leaderboard_as_admin(
        str(school_a.id), class_id=None, limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].display_name == "Student A"


# --- audit-log (school-scoped) ------------------------------------------------


async def test_list_school_audit_log_as_admin_basic_shape(db_session):
    from src.repositories import audit_repository

    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    teacher = await _seed_teacher(db_session, school.id)
    await audit_repository.record(
        db_session, actor_id=teacher.id, action="test.action", target_type="user", target_id=str(teacher.id),
    )
    await db_session.flush()

    result = await admin.list_school_audit_log_as_admin(
        str(school.id), limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].action == "test.action"
    assert result.items[0].actor_name == teacher.display_name


async def test_list_school_audit_log_as_admin_no_cross_school_leakage(db_session):
    from src.repositories import audit_repository

    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    teacher_a = await _seed_teacher(db_session, school_a.id)
    teacher_b = await _seed_teacher(db_session, school_b.id)
    await audit_repository.record(
        db_session, actor_id=teacher_a.id, action="school_a.action", target_type="user", target_id=str(teacher_a.id),
    )
    await audit_repository.record(
        db_session, actor_id=teacher_b.id, action="school_b.action", target_type="user", target_id=str(teacher_b.id),
    )
    await db_session.flush()

    result = await admin.list_school_audit_log_as_admin(
        str(school_a.id), limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].action == "school_a.action"


# --- question-bank -------------------------------------------------------------


async def test_list_school_question_bank_as_admin_merges_paper_and_homework_sources(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    topic = Topic(chapter_id=chapter.id, name="Photosynthesis")
    db_session.add(topic)
    await db_session.flush()

    teacher = await _seed_teacher(db_session, school.id)
    paper = await _seed_question_paper(db_session, school.id, subject, teacher)
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

    result = await admin.list_school_question_bank_as_admin(
        str(school.id), source=None, topic=None, collection_name=None, general_bank_only=False,
        limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 2
    by_source = {item.source: item for item in result.items}
    assert by_source["QUESTION_PAPER"].topic_label == "Photosynthesis"
    assert by_source["HOMEWORK"].topic_label == "Photosynthesis basics"


async def test_list_school_question_bank_as_admin_topic_filters(db_session):
    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
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

    matching = await admin.list_school_question_bank_as_admin(
        str(school.id), source=None, topic="basics", collection_name=None, general_bank_only=False,
        limit=50, offset=0, _=super_admin, session=db_session,
    )
    assert matching.total == 1

    no_match = await admin.list_school_question_bank_as_admin(
        str(school.id), source=None, topic="nonexistent topic", collection_name=None, general_bank_only=False,
        limit=50, offset=0, _=super_admin, session=db_session,
    )
    assert no_match.total == 0


async def test_list_school_question_bank_as_admin_collection_name_and_general_bank_only_filter(db_session):
    from src.domain.models import Dataset, DatasetQuestion, QuestionType

    super_admin = await _seed_super_admin(db_session)
    school = await _seed_school(db_session)
    school_class, subject, chapter = await _seed_class_subject_chapter(db_session, school.id)
    teacher = await _seed_teacher(db_session, school.id)
    collection = f"Collection-{uuid.uuid4()}"
    # a school's own question banks are school-owned datasets; "General bank" = no collection
    named_bank = Dataset(name=collection, question_count=1, description="", subject_id=subject.id,
                         grade=school_class.grade, school_id=school.id)
    general_bank = Dataset(name="General bank", question_count=1, description="", subject_id=subject.id,
                           grade=school_class.grade, school_id=school.id)
    db_session.add_all([named_bank, general_bank])
    await db_session.flush()
    db_session.add_all([
        DatasetQuestion(dataset_id=bank.id, chapter_id=chapter.id, text=text, answer="Answer",
                        bloom_level=BloomLevel.REMEMBER, question_type=QuestionType.SHORT_ANSWER, created_by=teacher.id)
        for bank, text in ((named_bank, "Named-set question"), (general_bank, "General bank question"))
    ])
    await db_session.flush()

    named = await admin.list_school_question_bank_as_admin(
        str(school.id), source="CUSTOM", topic=None, collection_name=collection, general_bank_only=False,
        limit=50, offset=0, _=super_admin, session=db_session,
    )
    assert named.total == 1
    assert named.items[0].text == "Named-set question"

    general = await admin.list_school_question_bank_as_admin(
        str(school.id), source="CUSTOM", topic=None, collection_name=None, general_bank_only=True,
        limit=50, offset=0, _=super_admin, session=db_session,
    )
    assert general.total == 1
    assert general.items[0].text == "General bank question"


async def test_list_school_question_bank_as_admin_no_cross_school_leakage(db_session):
    super_admin = await _seed_super_admin(db_session)
    school_a = await _seed_school(db_session)
    school_b = await _seed_school(db_session)
    class_a, subject_a, chapter_a = await _seed_class_subject_chapter(db_session, school_a.id)
    class_b, subject_b, chapter_b = await _seed_class_subject_chapter(db_session, school_b.id)
    teacher_a = await _seed_teacher(db_session, school_a.id)
    teacher_b = await _seed_teacher(db_session, school_b.id)
    paper_a = await _seed_question_paper(db_session, school_a.id, subject_a, teacher_a, name="Paper A")
    paper_b = await _seed_question_paper(db_session, school_b.id, subject_b, teacher_b, name="Paper B")
    db_session.add_all([
        QuestionPaperQuestion(paper_id=paper_a.id, order=1, bloom_level=BloomLevel.REMEMBER, marks=2, text="Q A"),
        QuestionPaperQuestion(paper_id=paper_b.id, order=1, bloom_level=BloomLevel.REMEMBER, marks=2, text="Q B"),
    ])
    await db_session.flush()

    result = await admin.list_school_question_bank_as_admin(
        str(school_a.id), source=None, topic=None, collection_name=None, general_bank_only=False,
        limit=50, offset=0, _=super_admin, session=db_session,
    )

    assert result.total == 1
    assert result.items[0].source_name == "Paper A"
