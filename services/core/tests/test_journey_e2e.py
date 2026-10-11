"""End-to-end user journey (QA): Super Admin -> School Admin -> Teacher ->
Student -> Teacher -> oversight, through the real route functions against
the dev Postgres (rolled back per test). External services (Firebase, KG,
generation, colearner insight) are monkeypatched.

Each test rebuilds the world through the chained helpers below, so a failure
in one step never hides the steps after it.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from src.api.routes import (
    admin, admin_catalog, ai_assessor, curriculum, dataset, homework, practice_bank, question_paper, roster,
    school_admin, school_oversight, school_question_bank as bank, student_homework, teacher_dashboard, test_result,
    voice_test,
)
from src.api.schemas.admin import CreatePlanIn, CreateSchoolIn, InviteSchoolAdminIn
from src.api.schemas.admin_catalog import (
    CreateChapterIn, CreateDatasetIn, CreateDatasetQuestionIn, CreateSubjectIn, CreateTopicIn, SyncSubjectFromKgIn,
)
from src.api.schemas.ai_assessor import OpenSessionIn
from src.api.schemas.bloom import BloomTarget
from src.api.schemas.dataset import DatasetShare
from src.api.schemas.homework import AssignHomeworkIn, CreateHomeworkIn, UpdateHomeworkQuestionIn
from src.api.schemas.question_paper import CreateQuestionPaperIn, PaperSection
from src.api.schemas.roster import AssignmentTarget
from src.api.schemas.school_admin import (
    ClassAssignmentOut, CreateClassIn, CreateStudentIn, InviteTeacherIn, UpdateClassAssignmentsIn,
    UpdateSchoolCurriculumIn, UpdateSchoolDatasetsIn,
)
from src.api.schemas.voice_test import CreateTestIn
from src.domain.models import (
    AssignmentTargetMode, BloomLevel, HomeworkDifficulty, HomeworkStatus, QuestionPaperStatus, QuestionType, Role,
    TestStatus, User, UserStatus, VoiceTest,
)
from src.domain.models.question_paper import PaperDifficultyLevel
from src.domain.models.voice_test import VoiceTestPrepStatus
from src.services import generation_client, homework_content_resolver, kg_client
from src.services.homework_autogen import auto_generate_homework
from src.services.test_completion import record_test_completion
from src.services.user_provisioning import ProvisionedUser

BLOOMS = [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND, BloomLevel.APPLY, BloomLevel.ANALYZE]


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- fakes
def _patch_externals(monkeypatch, calls: dict):
    def fake_firebase(*, email, display_name, role):
        return ProvisionedUser(firebase_uid=f"fb-{uuid.uuid4()}", temp_password="Temp#1234")

    monkeypatch.setattr(school_admin, "create_firebase_user", fake_firebase)
    monkeypatch.setattr(admin, "create_firebase_user", fake_firebase)

    ref = _uid()

    async def fake_tree(*, board, grade, subject):
        return {"subject": subject, "chapters": [
            {"ref": f"{ref}-CH12", "number": 12, "name": "Electricity", "topics": [
                {"ref": f"{ref}-T1", "number": "12.1", "name": "12.1 Electric Current", "subtopics": []},
                {"ref": f"{ref}-T2", "number": "12.2", "name": "12.2 Resistance", "subtopics": []}]},
            {"ref": f"{ref}-CH13", "number": 13, "name": "Magnetic Effects", "topics": [
                {"ref": f"{ref}-T3", "number": "13.1", "name": "13.1 Magnetic Field", "subtopics": []}]},
        ]}

    async def fake_paper_questions(*, chapters, total_questions, bloom_distribution, **_kw):
        calls.setdefault("kg_paper", []).append(chapters)
        calls.setdefault("kg_paper_refs", []).append(_kw.get("reference_questions") or [])
        levels = list(bloom_distribution) or ["REMEMBER"]
        return [{"bloom_level": levels[i % len(levels)], "text": f"KG paper question {i}",
                 "chapter_number": chapters[0]["number"]} for i in range(total_questions)]

    async def fake_assessor(**_kw):
        return {"reference_questions": [], "textbook_context": ""}

    async def fake_legacy(*_a, **_kw):
        calls.setdefault("legacy_fill", []).append(_kw)
        return []

    async def fake_insight(**kw):
        return {"what_needs_understanding": f"Revise {kw['topic']}", "references": [{"title": "T", "subtitle": "S"}],
                "key_idea_title": "K", "key_idea_body": "B", "connection_prompt": "C"}

    monkeypatch.setattr(kg_client, "get_curriculum_tree", fake_tree)
    monkeypatch.setattr(kg_client, "generate_paper_questions", fake_paper_questions)
    monkeypatch.setattr(kg_client, "generate_assessor_content", fake_assessor)
    monkeypatch.setattr(generation_client, "is_configured", lambda: False)
    monkeypatch.setattr(homework_content_resolver, "generate_questions", fake_legacy)
    monkeypatch.setattr(student_homework, "generate_homework_insight", fake_insight)


async def _user(db, out_id) -> User:
    return await db.get(User, uuid.UUID(str(out_id)))


# ---------------------------------------------------------------- step 1 + 2
async def _onboard_school(db, w, name):
    """Super Admin onboards a school + its School Admin (routes only)."""
    school = await admin.create_school(
        CreateSchoolIn(name=f"{name}-{_uid()}", board="CBSE", city="Pune", contact_email=f"{_uid()}@s.com",
                       plan_id=w.plan.id), actor=w.sa, session=db)
    sa_out = await admin.invite_school_admin(
        InviteSchoolAdminIn(school_id=school.id, email=f"admin-{_uid()}@s.com", display_name=f"{name} Admin"),
        actor=w.sa, session=db)
    return school, await _user(db, sa_out.id)


async def _new_class(db, admin_user, grade=10, section="A"):
    return await school_admin.create_school_class(CreateClassIn(grade=grade, section=section),
                                                  user=admin_user, session=db)


async def _new_teacher(db, admin_user, name):
    out = await school_admin.invite_teacher(InviteTeacherIn(email=f"t-{_uid()}@s.com", display_name=name),
                                            user=admin_user, session=db)
    return await _user(db, out.id)


async def _new_student(db, admin_user, class_id, name, roll):
    out = await school_admin.create_student(
        CreateStudentIn(display_name=name, email=f"st-{_uid()}@s.com", class_id=class_id, roll_number=roll),
        user=admin_user, session=db)
    return await _user(db, out.id)


async def _world(db, monkeypatch) -> SimpleNamespace:
    w = SimpleNamespace(calls={})
    _patch_externals(monkeypatch, w.calls)

    # 1. Super Admin: plan, school, subject Science/10, curriculum (KG sync + manual), catalog dataset
    w.sa = User(firebase_uid=f"sa-{uuid.uuid4()}", email=f"sa-{_uid()}@nool.in", display_name="Super",
                role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE)
    db.add(w.sa)
    await db.flush()
    w.plan = await admin.create_plan(CreatePlanIn(name=f"E2E-{_uid()}", price_label="Free", teacher_limit=20,
                                                  student_limit=200), actor=w.sa, session=db)
    w.school, w.admin = await _onboard_school(db, w, "Alpha")
    w.subject = await admin_catalog.create_subject(CreateSubjectIn(name=f"Science E2E {_uid()}", grade=10),
                                                   actor=w.sa, session=db)
    w.sync = await admin_catalog.sync_subject_from_kg(w.subject.id, SyncSubjectFromKgIn(grade=10),
                                                      actor=w.sa, session=db)
    chapters = (await admin_catalog.list_chapters(w.subject.id, _=w.sa, session=db)).items
    w.ch1 = next(c for c in chapters if c.name == "Electricity")
    w.ch2 = next(c for c in chapters if c.name == "Magnetic Effects")
    topics = (await admin_catalog.list_topics(w.ch1.id, _=w.sa, session=db)).items
    w.t1 = next(t for t in topics if t.name.startswith("12.1"))
    w.t2 = next(t for t in topics if t.name.startswith("12.2"))
    w.manual_ch = await admin_catalog.create_chapter(w.subject.id, CreateChapterIn(name="Manual Chapter"),
                                                     actor=w.sa, session=db)
    w.manual_topic = await admin_catalog.create_topic(w.manual_ch.id, CreateTopicIn(name="Manual Topic"),
                                                      actor=w.sa, session=db)

    w.catalog = await admin_catalog.create_dataset(
        CreateDatasetIn(name=f"Catalog Science 10 {_uid()}", question_count=0, description="QA bank",
                        subject_id=w.subject.id, grade=10, board="CBSE"), actor=w.sa, session=db)
    for level in BLOOMS:
        for i in range(3):
            await admin_catalog.create_dataset_question(
                w.catalog.id, CreateDatasetQuestionIn(
                    chapter_id=w.ch1.id, topic_id=w.t1.id, bloom_level=level,
                    question_type=QuestionType.SHORT_ANSWER,
                    difficulty=PaperDifficultyLevel.EASY if i < 2 else PaperDifficultyLevel.MEDIUM, marks=2,
                    text=f"{level.value} catalog question {i} on electric current?", answer=f"Answer {level.value} {i}"),
                actor=w.sa, session=db)
    w.catalog = await admin_catalog.get_dataset(w.catalog.id, _=w.sa, session=db)  # re-read: count is live
    await admin.update_curriculum_as_admin(w.school.id, UpdateSchoolCurriculumIn(subject_ids=[w.subject.id]),
                                           actor=w.sa, session=db)
    await admin.update_datasets_as_admin(w.school.id, UpdateSchoolDatasetsIn(dataset_ids=[w.catalog.id]),
                                         actor=w.sa, session=db)

    # 2. School Admin: class 10-A, teacher, 3 students, assignment, own bank + question
    w.cls = await _new_class(db, w.admin)
    w.teacher = await _new_teacher(db, w.admin, "Tara Teacher")
    w.students = [await _new_student(db, w.admin, w.cls.id, n, r)
                  for r, n in enumerate(["Asha", "Bala", "Chitra"], start=1)]
    await school_admin.update_class_assignments(
        w.cls.id, UpdateClassAssignmentsIn(assignments=[
            ClassAssignmentOut(teacher_id=str(w.teacher.id), subject_id=w.subject.id)]),
        user=w.admin, session=db)
    w.own = await bank.create_school_dataset(
        bank.CreateSchoolDatasetIn(name=f"Alpha Science 10 {_uid()}", subject_id=w.subject.id, grade=10),
        user=w.admin, session=db)
    w.own_q = await bank.create_school_dataset_question(
        w.own.id, CreateDatasetQuestionIn(
            chapter_id=w.ch1.id, topic_id=w.t1.id, bloom_level=BloomLevel.REMEMBER,
            question_type=QuestionType.SHORT_ANSWER, difficulty=PaperDifficultyLevel.EASY,
            text="School's own: define electric current.", answer="Rate of flow of charge"),
        user=w.admin, session=db)
    return w


# ---------------------------------------------------------------- step 3
async def _scheduled_test(db, w):
    created = await voice_test.create_test(
        CreateTestIn(class_id=w.cls.id, subject_id=w.subject.id, chapter_id=w.ch1.id, topic_id=w.t1.id,
                     bloom_levels=[BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], duration_minutes=10,
                     completion_window_hours=48, target=AssignmentTarget(mode=AssignmentTargetMode.WHOLE_CLASS)),
        background_tasks=BackgroundTasks(), user=w.teacher, _feature=w.teacher, session=db)
    # The question-prep background task never runs in this direct-call test;
    # settle it the way prep does when no dataset question fits, so sessions may start.
    (await db.get(VoiceTest, uuid.UUID(str(created.id)))).prep_status = VoiceTestPrepStatus.FAILED
    w.test = await voice_test.schedule_test(created.id, user=w.teacher, _feature=w.teacher, session=db)
    return w.test


# ---------------------------------------------------------------- step 4
async def _complete(db, w, student, answered, total=4):
    session = await ai_assessor.open_session(
        OpenSessionIn(test_id=w.test.id, context_label="Electricity", bloom_levels=w.test.bloom_levels,
                      duration_seconds=600), app_user=student, db=db)
    mastery = await record_test_completion(
        db, student_id=student.id, test_id=uuid.UUID(w.test.id), answered=answered, total_questions=total,
        bloom_levels=w.test.bloom_levels)
    await db.flush()
    if mastery is not None:
        test = await db.get(VoiceTest, uuid.UUID(w.test.id))
        await auto_generate_homework(db, test=test, student_id=student.id, mastery_percent=mastery,
                                     bloom_levels=w.test.bloom_levels)
        await db.flush()
    return session, mastery


async def _completed(db, w):
    await _scheduled_test(db, w)
    await _complete(db, w, w.students[0], answered=2)   # 50%
    await _complete(db, w, w.students[1], answered=3)   # 75%


# ---------------------------------------------------------------- step 5
async def _teacher_homework(db, w):
    kw = dict(user=w.teacher, _feature=w.teacher, session=db)
    w.hw = await homework.create_homework(
        CreateHomeworkIn(test_id=w.test.id, dataset_ids=[w.catalog.id], total_questions=4,
                         difficulty=HomeworkDifficulty.EASY,
                         bloom_distribution=[BloomTarget(level=BloomLevel.REMEMBER, value=50),
                                             BloomTarget(level=BloomLevel.UNDERSTAND, value=50)]), **kw)
    w.hw = await homework.generate_homework(w.hw.id, **kw)
    w.hw_questions = (await homework.list_homework_questions(w.hw.id, **kw)).items
    return w.hw


# ---------------------------------------------------------------- step 6
def _paper_body(w, **over):
    return CreateQuestionPaperIn(**{
        "name": "Unit Test 1", "exam_type": "UNIT_TEST", "board": "CBSE", "grade": 10, "language": "English",
        "subject_id": w.subject.id, "chapter_ids": [w.ch1.id], "topic_ids": [w.t1.id],
        "dataset_shares": [DatasetShare(dataset_id=w.catalog.id, percent=100)],
        "sections": [PaperSection(label="A", question_count=4, marks_each=2)], "total_marks": 8,
        "duration_minutes": 30,
        "bloom_distribution": [BloomTarget(level=BloomLevel.REMEMBER, value=50),
                               BloomTarget(level=BloomLevel.APPLY, value=50)], **over})


async def _paper(db, w):
    kw = dict(user=w.teacher, _feature=w.teacher, session=db)
    w.paper = await question_paper.create_paper(_paper_body(w), **kw)
    w.paper = await question_paper.generate_paper(w.paper.id, **kw)
    return w.paper


def _oversight_kw(user):
    return dict(limit=50, offset=0, user=user)


# ================================================================ tests
async def test_step1_2_super_admin_and_school_admin_setup(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)

    # Super Admin: subject is Science/10 only, KG sync gave grade-10 chapters
    assert (w.subject.grade, w.subject.grades) == (10, [10])
    assert (w.sync.chapters_synced, w.sync.topics_synced) == (2, 3)
    assert w.catalog.question_count == 12 and w.catalog.school_id is None
    admin_view = await admin.get_curriculum_as_admin(w.school.id, _=w.sa, session=db)
    assert {(s.id, s.enabled) for s in admin_view.subjects if s.id == w.subject.id} == {(w.subject.id, True)}

    # School Admin: curriculum is read-only and shows exactly Science 10
    cur = await school_admin.get_school_curriculum(user=w.admin, session=db)
    assert [(s.id, s.grade) for s in cur.subjects] == [(w.subject.id, 10)]
    assert not hasattr(school_admin, "update_school_curriculum")      # no school-side write route
    listing = await school_admin.get_school_datasets(user=w.admin, session=db)
    by_id = {d.id: d for d in listing.items}
    assert set(by_id) == {w.catalog.id, w.own.id}
    assert (by_id[w.catalog.id].owned, by_id[w.catalog.id].enabled) == (False, True)
    assert by_id[w.own.id].owned and by_id[w.own.id].subject_enabled
    classes = await school_admin.list_school_classes(user=w.admin, session=db)
    (c,) = [c for c in classes.items if c.id == w.cls.id]
    assert c.student_count == 3 and [a.teacher_id for a in c.assignments] == [str(w.teacher.id)]
    assert w.own_q.chapter_id == w.ch1.id

    # the School Admin can read the curriculum tree of an enabled subject
    chapters = await curriculum.list_chapters(w.subject.id, grade=10, user=w.admin, session=db)
    assert {w.ch1.id, w.ch2.id} <= {ch.id for ch in chapters.items}


async def test_step1_manual_chapter_gets_the_subjects_grade(db_session, monkeypatch):
    """A subject has exactly one grade - a chapter a Super Admin adds by hand
    under Science/10 should be a grade-10 chapter like the KG-synced ones."""
    db = db_session
    w = await _world(db, monkeypatch)
    chapters = await curriculum.list_chapters(w.subject.id, grade=10, user=w.admin, session=db)
    grades = {ch.id: ch.grade for ch in chapters.items}
    assert grades[w.ch1.id] == 10
    assert grades[w.manual_ch.id] == 10


async def test_step3_teacher_prepares_and_schedules_voice_test(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    t = w.teacher

    classes = await roster.list_classes(user=t, session=db)
    assert [c.id for c in classes.items] == [w.cls.id] and classes.items[0].student_count == 3
    subjects = await curriculum.list_subjects_for_class(w.cls.id, user=t, session=db)
    assert [s.id for s in subjects.items] == [w.subject.id]
    chapters = await curriculum.list_chapters(w.subject.id, grade=10, user=t, session=db)
    assert {w.ch1.id, w.ch2.id} <= {c.id for c in chapters.items}
    topics = await curriculum.list_topics(w.ch1.id, user=t, session=db)
    assert {w.t1.id, w.t2.id} == {x.id for x in topics.items}
    datasets = await dataset.list_datasets(subject_id=w.subject.id, grade=10, user=t, session=db)
    assert {d.id for d in datasets.items} == {w.catalog.id, w.own.id}
    students = await roster.list_class_students(w.cls.id, user=t, session=db)
    assert students.total == 3

    test = await _scheduled_test(db, w)
    assert test.status == TestStatus.SCHEDULED and test.assigned_count == 3
    listed = await voice_test.list_tests(user=t, _feature=t, session=db)
    assert test.id in {x.id for x in listed.items}
    dash = await teacher_dashboard.get_teacher_dashboard(class_id=None, user=t, session=db)
    assert dash.headline_test and dash.headline_test.id == test.id


async def test_step4_student_session_completion_auto_homework_and_practice_bank(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _scheduled_test(db, w)
    s1, s2, s3 = w.students

    opened, mastery = await _complete(db, w, s1, answered=2)
    assert opened.session_id and mastery == 50
    # a replay of the same completion does not create a second homework
    assert (await _complete(db, w, s1, answered=2))[1] is None

    current = (await student_homework.get_current_homework(user=s1, session=db)).homework
    assert current is not None and current.total_questions == 5 and current.gap_topic == "Electricity"
    qs = (await student_homework.get_my_homework_questions(current.homework_id, user=s1, session=db)).items
    assert len(qs) == 5 and not w.calls.get("legacy_fill")   # all 5 came from the school's banks
    assert {q.dataset_id for q in qs} <= {w.catalog.id, w.own.id}
    ctx = await student_homework.get_homework_context(current.homework_id, user=s1, session=db)
    assert ctx.what_needs_understanding == "Revise Electricity"
    with pytest.raises(NotFoundError):          # another student can't open s1's personal homework
        await student_homework.get_my_homework_questions(current.homework_id, user=s3, session=db)

    tasks = BackgroundTasks()
    confirmed = await student_homework.confirm_completion(current.homework_id, background_tasks=tasks, user=s1, session=db)
    # homework done unlocks this test's MCQ check and starts building it, so the app can open it next
    assert confirmed.mcq_test_id and [t.func.__name__ for t in tasks.tasks] == ["prebuild"]
    assert (await student_homework.get_current_homework(user=s1, session=db)).homework is None
    pb = await practice_bank.list_my_practice_bank(subject_id=None, chapter_id=None, bloom_level=None,
                                                   user=s1, session=db)
    assert pb.total == 5 and {e.chapter_id for e in pb.items} == {w.ch1.id}
    pb_filtered = await practice_bank.list_my_practice_bank(subject_id=w.subject.id, chapter_id=None,
                                                            bloom_level=BloomLevel.REMEMBER, user=s1, session=db)
    assert 0 < pb_filtered.total < 5
    assert (await practice_bank.list_my_practice_bank(subject_id=None, chapter_id=None, bloom_level=None,
                                                      user=s2, session=db)).total == 0


async def test_step5_teacher_results_then_homework_generate_review_assign(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _completed(db, w)
    t, (s1, s2, s3) = w.teacher, w.students
    kw = dict(user=t, _feature=t, session=db)

    cls_result = await test_result.get_class_result(w.test.id, user=t, session=db)
    assert (cls_result.assigned_count, cls_result.completed_count) == (3, 2)
    assert cls_result.class_mastery_percent == 62 or cls_result.class_mastery_percent == 63
    per_student = await test_result.list_student_results(w.test.id, user=t, session=db)
    assert {r.student_id: r.mastery_percent for r in per_student.items} == {str(s1.id): 50, str(s2.id): 75}

    hw = await _teacher_homework(db, w)
    assert hw.status == HomeworkStatus.REVIEW and hw.dataset_ids == [w.catalog.id]
    qs = w.hw_questions
    assert len(qs) == 4 and {q.dataset_id for q in qs} == {w.catalog.id}
    assert sorted(q.bloom_level for q in qs) == sorted([BloomLevel.REMEMBER] * 2 + [BloomLevel.UNDERSTAND] * 2)
    assert not w.calls.get("legacy_fill")

    # review: edit an answer, replace one, delete one
    out = await homework.update_homework_question(hw.id, qs[0].id, UpdateHomeworkQuestionIn(answer="Edited"), **kw)
    assert next(q for q in out.items if q.id == qs[0].id).answer == "Edited"
    before = {q.text for q in qs}
    out = await homework.replace_homework_question(hw.id, qs[1].id, **kw)
    replaced = next(q for q in out.items if q.id == qs[1].id)
    assert replaced.text not in before and replaced.bloom_level == qs[1].bloom_level
    assert replaced.dataset_id == w.catalog.id
    out = await homework.delete_homework_question(hw.id, qs[3].id, **kw)
    assert [q.order for q in out.items] == [1, 2, 3]

    # assign to two specific students
    assigned = await homework.assign_homework(
        hw.id, AssignHomeworkIn(target=AssignmentTarget(mode=AssignmentTargetMode.SPECIFIC_STUDENTS,
                                                        student_ids=[str(s2.id), str(s3.id)]),
                                completion_window_hours=24), **kw)
    assert assigned.status == HomeworkStatus.ASSIGNED and assigned.assigned_count == 2 and assigned.total_questions == 3
    listed = {h.id: h for h in (await homework.list_homework(**kw)).items}
    mine = listed[hw.id]
    assert mine.target_student_names == ["Bala", "Chitra"] and mine.chapter_name == "Electricity"
    assert mine.target.student_ids == [str(s2.id), str(s3.id)]
    auto = [h for h in listed.values() if h.id != hw.id]
    assert len(auto) == 2 and all(h.target.mode == AssignmentTargetMode.SPECIFIC_STUDENTS for h in auto)

    # students: s3 sees it, s1 doesn't
    cur3 = (await student_homework.get_current_homework(user=s3, session=db)).homework
    assert cur3 and cur3.homework_id == hw.id and cur3.total_questions == 3
    got = (await student_homework.get_my_homework_questions(hw.id, user=s3, session=db)).items
    assert len(got) == 3 and not hasattr(got[0], "answer")  # students never get answers
    with pytest.raises(NotFoundError):
        await student_homework.get_my_homework(hw.id, user=s1, session=db)
    await student_homework.confirm_completion(hw.id, background_tasks=BackgroundTasks(), user=s3, session=db)
    after = await homework.get_homework(hw.id, **kw)
    assert (after.completed_count, after.status) == (1, HomeworkStatus.IN_PROGRESS)


async def test_step6_question_paper_create_generate_finalize(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    kw = dict(user=w.teacher, _feature=w.teacher, session=db)

    paper = await _paper(db, w)
    assert paper.dataset_shares == [DatasetShare(dataset_id=w.catalog.id, percent=100)]
    assert paper.status == QuestionPaperStatus.REVIEW, paper.validation
    assert w.calls["kg_paper"] == [[{"number": 12, "name": "Electricity"}]]
    qs = (await question_paper.list_paper_questions(paper.id, **kw)).items
    assert len(qs) == 4 and sum(q.marks for q in qs) == 8
    assert paper.validation.coverage_percent == 100
    final = await question_paper.finalize_paper(paper.id, **kw)
    assert final.status == QuestionPaperStatus.FINALIZED
    with pytest.raises(ConflictError):
        await question_paper.update_paper(paper.id, _paper_body(w, name="changed"), **kw)


async def test_step6_question_paper_uses_the_shared_dataset(db_session, monkeypatch):
    """A paper created 'with the dataset share' (100% catalog) should draw on
    that dataset's questions - not ignore it and only ask the KG. They go to
    the KG as reference_questions, which decides what to reuse verbatim (see
    question_paper._dataset_reference_questions)."""
    db = db_session
    w = await _world(db, monkeypatch)
    await _paper(db, w)
    (refs,) = w.calls["kg_paper_refs"]
    catalog_texts = {f"{lv.value} catalog question {i} on electric current?" for lv in BLOOMS for i in range(3)}
    assert refs and {r["text"] for r in refs} <= catalog_texts, refs


async def test_step7_school_admin_and_super_admin_oversight(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _completed(db, w)
    await _teacher_homework(db, w)
    await _paper(db, w)
    a = w.admin

    tests = await school_oversight.list_school_voice_tests(class_id=None, teacher_id=None, **_oversight_kw(a), session=db)
    (vt,) = tests.items
    assert (vt.id, vt.teacher_name, vt.completed_count, vt.assigned_count) == (w.test.id, "Tara Teacher", 2, 3)
    hws = await school_oversight.list_school_homework(class_id=None, **_oversight_kw(a), session=db)
    assert hws.total == 3 and w.hw.id in {h.id for h in hws.items}
    assert all(h.teacher_name == "Tara Teacher" for h in hws.items)
    papers = await school_oversight.list_school_question_papers(subject_id=None, created_by=None,
                                                                **_oversight_kw(a), session=db)
    assert [(p.id, p.created_by_name) for p in papers.items] == [(w.paper.id, "Tara Teacher")]
    qb = await school_oversight.list_school_question_bank(
        source=None, topic=None, collection_name=None, general_bank_only=False, dataset_id=None, limit=500,
        offset=0, user=a, session=db)
    sources = {e.source for e in qb.items}
    assert sources == {"QUESTION_PAPER", "HOMEWORK", "CUSTOM"}
    assert w.own_q.id in {e.id for e in qb.items if e.source == "CUSTOM"}
    board = await school_oversight.get_school_leaderboard(class_id=None, **_oversight_kw(a), session=db)
    assert board.items[0].display_name == "Bala" and board.items[0].points == 30
    log = await school_oversight.list_school_audit_log(limit=200, offset=0, user=a, session=db)
    assert {"class.created", "teacher.invited", "student.created", "school_dataset.created"} <= {e.action for e in log.items}

    # Super Admin: the same, cross-tenant
    sid = w.school.id
    assert (await admin.list_school_voice_tests_as_admin(sid, class_id=None, teacher_id=None, limit=50, offset=0,
                                                         _=w.sa, session=db)).total == 1
    assert (await admin.list_school_homework_as_admin(sid, class_id=None, limit=50, offset=0, _=w.sa,
                                                      session=db)).total == 3
    assert (await admin.list_school_question_papers_as_admin(sid, subject_id=None, created_by=None, limit=50,
                                                             offset=0, _=w.sa, session=db)).total == 1
    sqb = await admin.list_school_question_bank_as_admin(sid, source=None, topic=None, collection_name=None,
                                                         general_bank_only=False, dataset_id=None, limit=500,
                                                         offset=0, _=w.sa, session=db)
    assert sqb.total == qb.total
    ds = {d.id: d for d in (await admin.get_datasets_as_admin(sid, _=w.sa, session=db)).items}
    assert ds[w.catalog.id].enabled and ds[w.own.id].owned


async def test_step8a_another_schools_teacher_sees_none_of_it(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _completed(db, w)
    await _teacher_homework(db, w)
    await _paper(db, w)

    # school B: same subject enabled by the Super Admin, but no dataset assigned
    school_b, admin_b = await _onboard_school(db, w, "Beta")
    await admin.update_curriculum_as_admin(school_b.id, UpdateSchoolCurriculumIn(subject_ids=[w.subject.id]),
                                           actor=w.sa, session=db)
    cls_b = await _new_class(db, admin_b)
    teacher_b = await _new_teacher(db, admin_b, "Other Teacher")
    student_b = await _new_student(db, admin_b, cls_b.id, "Zed", 1)
    await school_admin.update_class_assignments(
        cls_b.id, UpdateClassAssignmentsIn(assignments=[
            ClassAssignmentOut(teacher_id=str(teacher_b.id), subject_id=w.subject.id)]), user=admin_b, session=db)
    kw = dict(user=teacher_b, _feature=teacher_b, session=db)

    assert [c.id for c in (await roster.list_classes(user=teacher_b, session=db)).items] == [cls_b.id]
    assert (await dataset.list_datasets(subject_id=None, grade=None, user=teacher_b, session=db)).items == []
    assert (await voice_test.list_tests(**kw)).items == []
    assert (await homework.list_homework(**kw)).items == []
    assert w.paper.id not in {p.id for p in (await question_paper.list_papers(**kw)).items}
    for call in (lambda: voice_test.get_test(w.test.id, **kw),
                 lambda: homework.get_homework(w.hw.id, **kw),
                 lambda: question_paper.get_paper(w.paper.id, **kw),
                 lambda: test_result.get_class_result(w.test.id, user=teacher_b, session=db),
                 lambda: roster.list_class_students(w.cls.id, user=teacher_b, session=db)):
        with pytest.raises(NotFoundError):
            await call()
    with pytest.raises(NotFoundError):
        await ai_assessor.open_session(OpenSessionIn(test_id=w.test.id, context_label="x",
                                                     bloom_levels=[BloomLevel.REMEMBER], duration_seconds=60),
                                       app_user=student_b, db=db)
    with pytest.raises(NotFoundError):
        await student_homework.get_my_homework(w.hw.id, user=student_b, session=db)
    with pytest.raises(NotFoundError):     # can't read school A's own bank
        await bank.list_school_dataset_questions(w.own.id, chapter_id=None, topic_id=None, subtopic_id=None,
                                                 bloom_level=None, difficulty=None, limit=100, offset=0,
                                                 user=admin_b, session=db)
    # school B's admin sees none of A's activity or banks
    b_ds = await school_admin.get_school_datasets(user=admin_b, session=db)
    assert b_ds.items == []
    assert (await school_oversight.list_school_homework(class_id=None, **_oversight_kw(admin_b), session=db)).total == 0
    assert (await school_oversight.list_school_question_bank(
        source=None, topic=None, collection_name=None, general_bank_only=False, dataset_id=None, limit=50,
        offset=0, user=admin_b, session=db)).total == 0


async def test_step8b_a_teacher_of_the_class_but_not_the_subject_cant_act(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _completed(db, w)
    await _teacher_homework(db, w)
    await _paper(db, w)

    # Maths/10 also enabled; teacher2 teaches Maths in the same class 10-A
    maths = await admin_catalog.create_subject(CreateSubjectIn(name=f"Maths E2E {_uid()}", grade=10),
                                               actor=w.sa, session=db)
    await admin.update_curriculum_as_admin(w.school.id, UpdateSchoolCurriculumIn(subject_ids=[w.subject.id, maths.id]),
                                           actor=w.sa, session=db)
    t2 = await _new_teacher(db, w.admin, "Maths Teacher")
    await school_admin.update_class_assignments(
        w.cls.id, UpdateClassAssignmentsIn(assignments=[
            ClassAssignmentOut(teacher_id=str(w.teacher.id), subject_id=w.subject.id),
            ClassAssignmentOut(teacher_id=str(t2.id), subject_id=maths.id)]), user=w.admin, session=db)
    kw = dict(user=t2, _feature=t2, session=db)

    with pytest.raises(ForbiddenError):
        await homework.create_homework(CreateHomeworkIn(test_id=w.test.id, dataset_ids=[w.catalog.id],
                                                        total_questions=2, difficulty=HomeworkDifficulty.EASY,
                                                        bloom_distribution=[]), **kw)
    with pytest.raises(ForbiddenError):
        await homework.get_homework(w.hw.id, **kw)
    with pytest.raises(ForbiddenError):
        await test_result.get_class_result(w.test.id, user=t2, session=db)
    assert (await homework.list_homework(**kw)).items == []
    assert (await voice_test.list_tests(**kw)).items == []
    assert (await dataset.list_datasets(subject_id=None, grade=None, user=t2, session=db)).items == []
    with pytest.raises(NotFoundError):
        await curriculum.list_chapters(w.subject.id, grade=None, user=t2, session=db)
    with pytest.raises(ForbiddenError):
        await question_paper.create_paper(_paper_body(w), **kw)


async def test_step8b_a_teacher_not_teaching_the_subject_cant_generate_or_finalize_its_paper(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _paper(db, w)
    t2 = await _new_teacher(db, w.admin, "Unassigned Teacher")    # teaches nothing
    kw = dict(user=t2, _feature=t2, session=db)
    with pytest.raises((ForbiddenError, NotFoundError)):
        await question_paper.generate_paper(w.paper.id, **kw)
    with pytest.raises((ForbiddenError, NotFoundError)):
        await question_paper.finalize_paper(w.paper.id, **kw)


async def test_step8c_disabling_the_subject_hides_its_datasets_from_the_teacher(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _completed(db, w)
    t = w.teacher
    kw = dict(user=t, _feature=t, session=db)
    assert {d.id for d in (await dataset.list_datasets(subject_id=None, grade=None, user=t, session=db)).items} == \
        {w.catalog.id, w.own.id}

    await admin.update_curriculum_as_admin(w.school.id, UpdateSchoolCurriculumIn(subject_ids=[]), actor=w.sa, session=db)
    assert (await dataset.list_datasets(subject_id=None, grade=None, user=t, session=db)).items == []
    assert (await curriculum.list_subjects_for_class(w.cls.id, user=t, session=db)).items == []
    assert (await school_admin.get_school_curriculum(user=w.admin, session=db)).subjects == []
    with pytest.raises(NotFoundError):
        await curriculum.list_chapters(w.subject.id, grade=10, user=t, session=db)
    with pytest.raises(ForbiddenError):   # the catalog dataset is no longer usable for homework
        await homework.create_homework(CreateHomeworkIn(test_id=w.test.id, dataset_ids=[w.catalog.id],
                                                        total_questions=2, difficulty=HomeworkDifficulty.EASY,
                                                        bloom_distribution=[]), **kw)
    listing = {d.id: d for d in (await school_admin.get_school_datasets(user=w.admin, session=db)).items}
    assert not listing[w.catalog.id].subject_enabled and not listing[w.own.id].subject_enabled

    # re-enabling restores them without re-assigning
    await admin.update_curriculum_as_admin(w.school.id, UpdateSchoolCurriculumIn(subject_ids=[w.subject.id]),
                                           actor=w.sa, session=db)
    assert {d.id for d in (await dataset.list_datasets(subject_id=None, grade=None, user=t, session=db)).items} == \
        {w.catalog.id, w.own.id}
