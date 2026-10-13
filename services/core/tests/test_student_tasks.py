"""The Tests tab: each topic loop and teacher voice test as a chain of steps
with the one waiting on the student, the early-finish homework bonus, and
its XP paid once on completion.

Real rows against the dev Postgres via db_session (seeded like the topic
quiz and MCQ check tests); no model calls."""

from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks

from src.api.routes import student_homework
from src.api.routes import student_tasks as routes
from src.domain.models import (
    AssignmentTargetMode,
    Homework,
    HomeworkDifficulty,
    HomeworkStatus,
    HomeworkTargetStudent,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    StudentPoints,
    TestStatus as VoiceTestStatus,
    VoiceTestTargetStudent,
)
from src.services import student_tasks as service
from src.services import topic_quiz
from tests.test_mcq_test import _seed as _seed_voice_test
from tests.test_topic_quiz import R, U, _answer, _finish, _homework_done, _pools, _seed, _start, _stub_analysis, _teach

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


def test_early_bonus_by_share_of_the_window_used():
    assert service.early_bonus_xp(NOW, 12, NOW + timedelta(hours=2)) == 30  # first quarter
    assert service.early_bonus_xp(NOW, 12, NOW + timedelta(hours=6)) == 20  # by half time
    assert service.early_bonus_xp(NOW, 12, NOW + timedelta(hours=11)) == 10  # before it's due
    assert service.early_bonus_xp(NOW, 12, NOW + timedelta(hours=13)) == 0  # late
    assert service.early_bonus_xp(NOW, None, NOW) == 0  # no window, no bonus


def _steps(task):
    return [(s.kind, s.status) for s in task.steps]


async def _task(db, student, task_id):
    return next(t for t in (await routes.get_my_tasks(user=student, session=db)).tasks if t.id == task_id)


async def test_topic_task_walks_quiz_homework_recheck(db_session, monkeypatch):
    s = await _seed(db_session)
    await _teach(db_session, s, s.ohm)
    await _pools(db_session, s.ohm)
    _stub_analysis(monkeypatch, [])
    tid = f"topic:{s.ohm.id}"

    task = await _task(db_session, s.student, tid)
    assert task.state == "open" and task.subtitle == "Topic checkpoint" and task.subject.startswith("Science")
    assert (task.chapter_name, task.topic_id) == ("Electricity", str(s.ohm.id))  # the Learn tab groups by chapter
    assert _steps(task) == [("quiz", "current"), ("homework", "locked"), ("recheck", "locked")]

    quiz = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, quiz, {R, U})
    await _finish(db_session, s, quiz)
    task = await _task(db_session, s.student, tid)
    assert _steps(task) == [("quiz", "done"), ("homework", "waiting"), ("recheck", "locked")]
    assert task.steps[0].note == "50%" and task.steps[1].note == "Being made for you"

    hw = await _homework_done(db_session, s, quiz.id, StudentHomeworkStatus.ASSIGNED)
    hw.completion_window_hours = topic_quiz.HOMEWORK_WINDOW_HOURS
    await db_session.flush()
    task = await _task(db_session, s.student, tid)
    assert _steps(task)[1] == ("homework", "current") and task.steps[1].target_id == str(hw.id)
    assert task.steps[1].due_at == hw.created_at + timedelta(hours=12)

    done = await student_homework.confirm_completion(str(hw.id), background_tasks=BackgroundTasks(),
                                                     user=s.student, session=db_session)
    assert done.recheck_topic_id == str(s.ohm.id) and done.early_bonus_xp == 30  # done straight away
    task = await _task(db_session, s.student, tid)
    assert _steps(task) == [("quiz", "done"), ("homework", "done"), ("recheck", "current")]
    assert task.steps[1].note == "Done early · +30 XP"


async def test_mastered_at_the_first_quiz_skips_homework_and_moves_down(db_session, monkeypatch):
    s = await _seed(db_session)
    for topic in (s.ohm, s.power):
        await _teach(db_session, s, topic)
        await _pools(db_session, topic)
    _stub_analysis(monkeypatch, [])
    quiz = await _start(db_session, s, s.ohm)
    await _answer(db_session, s, quiz, {"REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE"})
    await _finish(db_session, s, quiz)

    tasks = (await routes.get_my_tasks(user=s.student, session=db_session)).tasks
    assert [t.id for t in tasks] == [f"topic:{s.power.id}", f"topic:{s.ohm.id}"]  # open first, then done
    ohm = tasks[1]
    assert ohm.state == "done" and ohm.result == "Mastered"
    assert _steps(ohm) == [("quiz", "done"), ("homework", "skipped"), ("recheck", "skipped")]


async def test_voice_test_task_runs_test_homework_mcq_check(db_session):
    s = await _seed_voice_test(db_session, taken_hours_ago=1)
    s["test"].status = VoiceTestStatus.ACTIVE
    db_session.add(VoiceTestTargetStudent(test_id=s["test"].id, student_id=s["student"].id))
    await db_session.flush()
    tid = f"test:{s['test'].id}"

    task = await _task(db_session, s["student"], tid)
    assert task.kind == "test" and task.title == "Electricity" and task.subject.startswith("Science")
    assert _steps(task) == [("voice_test", "done"), ("homework", "waiting"), ("mcq_check", "waiting")]

    hw = Homework(test_id=s["test"].id, class_id=s["class"].id, gap_topic="Electricity", gap_mastery_percent=46,
                  total_questions=5, difficulty=HomeworkDifficulty.MIXED,
                  target_mode=AssignmentTargetMode.SPECIFIC_STUDENTS, completion_window_hours=48,
                  status=HomeworkStatus.ASSIGNED, assigned_count=1,
                  created_at=datetime.now(timezone.utc) - timedelta(hours=30))
    db_session.add(hw)
    await db_session.flush()
    db_session.add_all([HomeworkTargetStudent(homework_id=hw.id, student_id=s["student"].id),
                        StudentHomeworkProgress(homework_id=hw.id, student_id=s["student"].id)])
    await db_session.flush()
    task = await _task(db_session, s["student"], tid)
    assert task.steps[1].status == "current" and task.steps[1].note is None

    done = await student_homework.confirm_completion(str(hw.id), background_tasks=BackgroundTasks(),
                                                     user=s["student"], session=db_session)
    assert done.early_bonus_xp == 10  # 30 of 48 hours used: before it's due
    points = await db_session.get(StudentPoints, s["student"].id)
    task = await _task(db_session, s["student"], tid)
    assert _steps(task) == [("voice_test", "done"), ("homework", "done"), ("mcq_check", "current")]
    again = await student_homework.confirm_completion(str(hw.id), background_tasks=BackgroundTasks(),
                                                      user=s["student"], session=db_session)
    assert again.early_bonus_xp == 0  # paid once
    assert (await db_session.get(StudentPoints, s["student"].id)).points == points.points
