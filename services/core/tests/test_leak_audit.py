"""A leak audit over EVERY route that takes an id: two schools, several
sections, teachers who each teach one class - and each caller probes every
route with ids that belong to someone else (another section's class, test,
homework, paper, student; another school). Anything but a refusal (4xx) is
a leak and fails the test, listed route by route.

Routes whose ids are only shared curriculum (subjects, chapters, topics)
are skipped - those are the same for every school. A 422 (the body didn't
validate) is counted as a refusal only for writes: the route never reached
the data. A 200 that hands nothing over is not a leak either: an empty
list, or an idempotent DELETE that touched nothing (the other school's rows
are checked to still be there at the end).

Question papers are deliberately shared: anyone in the school may read one,
teachers of its subject + grade may change it (question_paper.py
_get_paper_to_edit). So the victim paper is a Maths one: T1 (Science) may
read it but never change it; T3 (Maths 10) is a co-teacher.
"""

import re
import uuid

import httpx
import pytest

from src.api import deps
from src.domain.models import (
    AssignmentTargetMode,
    Chapter,
    DailyPracticeSession,
    Homework,
    HomeworkDifficulty,
    HomeworkStatus,
    HomeworkTargetStudent,
    QuizTest,
    Role,
    School,
    SchoolClass,
    StudentHomeworkProgress,
    StudentProfile,
    Subject,
    TeacherClassAssignment,
    Topic,
    User,
    UserStatus,
    VoiceTest,
)
from src.domain.models.question_paper import QuestionPaper, QuestionPaperStatus
from src.domain.models.voice_test import TestStatus
from src.services import generation_client, push

SHARED = {"subject_id", "chapter_id", "topic_id", "subtopic_id", "grade_id"}  # curriculum: the same for everyone
NO_ID = {"token", "request_id", "job_id", "version_id", "share_id"}  # opaque / non-tenant values


def _user(school, role, name):
    return User(firebase_uid=f"{name}-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=name,
                role=role, school_id=school.id, status=UserStatus.ACTIVE)


async def _world(db):
    """School A: 10A and 10B. T1 teaches 10A Science, T2 10B Science, T3 10A
    Maths; S1 is in 10A, S2 in 10B. School B: its own 10A, teacher TB, student SB."""
    a = School(name=f"A-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@a.com")
    b = School(name=f"B-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email="b@b.com")
    science, maths = Subject(name=f"Science-{uuid.uuid4()}"), Subject(name=f"Mathematics-{uuid.uuid4()}")
    db.add_all([a, b, science, maths])
    await db.flush()
    a10a, a10b = SchoolClass(school_id=a.id, grade=10, section="A"), SchoolClass(school_id=a.id, grade=10, section="B")
    b10a = SchoolClass(school_id=b.id, grade=10, section="A")
    chapter = Chapter(subject_id=science.id, name="Electricity", order_index=11, grade=10)
    w = {name: _user(sch, role, name) for name, sch, role in [
        ("T1", a, Role.TEACHER), ("T2", a, Role.TEACHER), ("T3", a, Role.TEACHER), ("TB", b, Role.TEACHER),
        ("S1", a, Role.STUDENT), ("S2", a, Role.STUDENT), ("SB", b, Role.STUDENT), ("ADMIN_A", a, Role.SCHOOL_ADMIN)]}
    db.add_all([a10a, a10b, b10a, chapter, *w.values()])
    await db.flush()
    topic = Topic(chapter_id=chapter.id, name="11.4 Ohm's law")
    db.add_all([
        topic,
        TeacherClassAssignment(teacher_id=w["T1"].id, class_id=a10a.id, subject_id=science.id),
        TeacherClassAssignment(teacher_id=w["T2"].id, class_id=a10b.id, subject_id=science.id),
        TeacherClassAssignment(teacher_id=w["T3"].id, class_id=a10a.id, subject_id=maths.id),
        TeacherClassAssignment(teacher_id=w["TB"].id, class_id=b10a.id, subject_id=science.id),
        StudentProfile(user_id=w["S1"].id, class_id=a10a.id, roll_number=1),
        StudentProfile(user_id=w["S2"].id, class_id=a10b.id, roll_number=1),
        StudentProfile(user_id=w["SB"].id, class_id=b10a.id, roll_number=1),
    ])
    await db.flush()
    # what T2 / 10B / S2 own
    test = VoiceTest(class_id=a10b.id, subject_id=science.id, chapter_id=chapter.id, duration_minutes=10,
                     completion_window_hours=24, target_mode=AssignmentTargetMode.WHOLE_CLASS, status=TestStatus.ACTIVE)
    quiz_test = QuizTest(class_id=a10b.id, subject_id=science.id, chapter_id=chapter.id, title="Ch 11",
                         status="published", created_by=w["T2"].id, topic_ids=[str(topic.id)], mix={"mcq": 4})
    paper = QuestionPaper(school_id=a.id, created_by=w["T2"].id, name="T2's paper", exam_type="Unit test",
                          board="CBSE", grade=10, language="English", subject_id=maths.id,
                          status=QuestionPaperStatus.DRAFT)
    homework = Homework(test_id=None, topic_id=topic.id, class_id=a10b.id, gap_topic="Ohm's law",
                        gap_mastery_percent=40, total_questions=5, difficulty=HomeworkDifficulty.MIXED,
                        target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS, status=HomeworkStatus.ASSIGNED)
    db.add_all([test, quiz_test, paper, homework])
    await db.flush()
    practice = DailyPracticeSession(student_id=w["S2"].id, practice_date=__import__("datetime").date.today(),
                                    chapter_id=chapter.id, topic_id=topic.id, attempt=1, status="open", xp_awarded=0)
    db.add_all([practice, HomeworkTargetStudent(homework_id=homework.id, student_id=w["S2"].id),
                StudentHomeworkProgress(homework_id=homework.id, student_id=w["S2"].id)])
    await db.flush()
    # path param -> (label, id) of what each caller must NOT reach: T2/10B/S2's things in
    # school A (another section), and school B's
    school_a_other = {
        "class_id": [("A 10B", a10b.id)], "test_id": [("T2 voice test", test.id), ("T2 quiz test", quiz_test.id)],
        "paper_id": [("T2 paper", paper.id)], "homework_id": [("10B homework", homework.id)],
        "teacher_id": [("T2", w["T2"].id)], "student_id": [("S2", w["S2"].id)],
        "session_id": [("S2 practice", practice.id)], "quiz_id": [("S2 quiz", practice.id)],
    }
    school_b = {"school_id": [("school B", b.id)], "class_id": [("B 10A", b10a.id)],
                "teacher_id": [("TB", w["TB"].id)], "student_id": [("SB", w["SB"].id)]}
    school_a = {"school_id": [("school A", a.id)], "class_id": [("A 10A", a10a.id), ("A 10B", a10b.id)],
                "teacher_id": [("T1", w["T1"].id)], "student_id": [("S1", w["S1"].id)],
                **{k: v for k, v in school_a_other.items() if k not in ("class_id", "teacher_id", "student_id")}}

    def merge(*maps):
        out = {}
        for m in maps:
            for k, v in m.items():
                out.setdefault(k, []).extend(v)
        return out

    no_paper = {k: v for k, v in school_a_other.items() if k != "paper_id"}
    victims = {
        "T1": merge(school_a_other, school_b), "T3": merge(no_paper, school_b),  # T3 co-teaches Maths 10
        "S1": merge(school_a_other, school_b, {"teacher_id": [("T1", w["T1"].id)]}),
        "TB": school_a, "SB": school_a,
        "ADMIN_A": school_b,  # a school admin runs their own school - just never another
    }
    return w, victims, {"subject_id": science.id, "chapter_id": chapter.id, "topic_id": topic.id,
                        "keep": [(User, w["TB"].id), (User, w["SB"].id), (SchoolClass, b10a.id)]}


def _routes(app):
    for route in app.routes:
        path = getattr(route, "path", "")
        params = re.findall(r"{(\w+)}", path)
        if not path.startswith("/api/v1") or not params or not set(params) - SHARED - NO_ID:
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            yield method, path, params


async def _probe(app, db, caller: User, other, shared) -> list[str]:
    app.dependency_overrides[deps.get_current_app_user] = lambda: caller
    app.dependency_overrides[deps.get_db_session] = lambda: db
    leaks = []
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://audit") as client:
        for method, path, params in _routes(app):
            tenant = [p for p in params if p not in SHARED and p not in NO_ID]
            for p in tenant:
                for label, victim in other.get(p, [("random", uuid.uuid4())]):
                    url = path
                    for q in params:
                        value = victim if q == p else (other.get(q) or [(None, None)])[0][1] or shared.get(q) or uuid.uuid4()
                        url = url.replace("{" + q + "}", str(value))
                    response = await client.request(method, url, json={})
                    ok = response.status_code < 400 or (response.status_code == 422 and method == "GET")
                    if ok and _hands_nothing(method, p, caller, response):
                        continue
                    if ok and response.status_code != 404:
                        leaks.append(f"{caller.display_name} {method} {path} [{p}={label}] -> {response.status_code}")
                    await db.rollback() if not db.in_transaction() else None
    return leaks


def _hands_nothing(method, param, caller, response) -> bool:
    """A 200 that gives the caller nothing of the victim's."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if method == "GET" and (body in ([], {}) or (isinstance(body, dict) and body.get("items") == [])):
        return True
    if method == "DELETE" and body == {"deleted": True}:
        return True  # idempotent no-op; the rows are checked afterwards
    # papers: readable school-wide by design (another school's never - the query checks school_id)
    return param == "paper_id" and method == "GET" and caller.role == Role.TEACHER


@pytest.fixture
def app(_core_env, monkeypatch):
    from src.main import create_app

    async def no_ai(*_a, **_k):
        raise generation_client.GenerationServiceUnavailableError("audit: no model calls")

    async def no_push(*_a, **_k):
        return 0

    monkeypatch.setattr(generation_client, "_post", no_ai)
    monkeypatch.setattr(push, "send_push", no_push)
    return create_app()


@pytest.mark.asyncio
async def test_no_route_hands_one_classes_or_schools_data_to_another(app, db_session):
    world, victims, shared = await _world(db_session)
    leaks = []
    for name in ("T1", "T3", "TB", "S1", "SB", "ADMIN_A"):
        nested = await db_session.begin_nested()
        leaks += await _probe(app, db_session, world[name], victims[name], shared)
        if nested.is_active:
            await nested.rollback()
    for model, row_id in shared["keep"]:  # school B's rows survived school A's deletes
        assert await db_session.get(model, row_id) is not None, f"{model.__name__} {row_id} was deleted"
    assert not leaks, "\n".join(leaks)


@pytest.mark.asyncio
async def test_the_audit_can_see_a_200_the_owner_still_gets_their_own(app, db_session):
    """Control: the same probe as the owner (T2, 10B) is let in - so the
    refusals above are the access rules, not a broken harness."""
    world, victims, shared = await _world(db_session)
    t2 = world["T2"]
    app.dependency_overrides[deps.get_current_app_user] = lambda: t2
    app.dependency_overrides[deps.get_db_session] = lambda: db_session
    a10b = victims["T1"]["class_id"][0][1]
    test_id = victims["T1"]["test_id"][0][1]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://audit") as client:
        assert (await client.get(f"/api/v1/classes/{a10b}/students")).status_code == 200
        assert (await client.get(f"/api/v1/tests/{test_id}")).status_code == 200


@pytest.mark.asyncio
async def test_no_list_shows_another_sections_or_schools_things(app, db_session):
    """Every GET without ids (the lists: my tests, homework, classes, students...)
    as each caller - none may mention the other section's or school's ids."""
    world, victims, shared = await _world(db_session)
    found = []
    for name in ("T1", "T3", "TB", "S1", "SB", "ADMIN_A"):
        caller = world[name]
        app.dependency_overrides[deps.get_current_app_user] = lambda c=caller: c
        app.dependency_overrides[deps.get_db_session] = lambda: db_session
        secret = {str(v): f"{p}={label}" for p, pairs in victims[name].items() for label, v in pairs
                  if p != "paper_id"}  # papers are school-wide by design
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://audit") as client:
            for route in app.routes:
                path = getattr(route, "path", "")
                if (not path.startswith("/api/v1") or "{" in path or "GET" not in (getattr(route, "methods", None) or [])
                        or "/admin/" in path):
                    continue
                response = await client.get(path)
                if response.status_code >= 400:
                    continue
                for value, what in secret.items():
                    if value in response.text:
                        found.append(f"{name} GET {path} shows {what}")
    assert not found, "\n".join(sorted(set(found)))
