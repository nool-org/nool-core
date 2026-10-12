"""The student's Tests tab: every assigned task as a chain of steps, with the
step that's waiting on them. Nothing here is stored - it's read from the
topic loops (topic_quiz.py), voice tests, their homework and MCQ checks.

- Topic task (each taught topic): quiz -> homework (12 h) -> re-check.
- Test task (each teacher voice test): voice test -> homework -> MCQ check.

A step is "done", "current" (the student's move), "waiting" (on us -
homework being made, MCQ check not unlocked yet), "locked" (after the
current one) or "skipped" (not needed - nothing lagged, so no homework).

Homework finished early earns a bonus (early_bonus_xp), worked out from
when it was set, its window and when it was done.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.assigned_test import taken_test_ids
from src.domain.models import (
    Chapter,
    Homework,
    HomeworkTargetStudent,
    McqTest,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
    Subject,
    TestStatus,
    User,
    VoiceTest,
    VoiceTestTargetStudent,
)
from src.services import daily_practice, mcq_test, topic_quiz

# (share of the window used, bonus): done in the first quarter -> 30 XP, ...
EARLY_BONUS = [(0.25, 30), (0.5, 20), (1.0, 10)]
DONE_SHOWN = 10  # finished tasks listed under the open ones


def early_bonus_xp(set_at: datetime, window_hours: int | None, done_at: datetime) -> int:
    """XP for finishing homework early - none without a window or once late."""
    if not window_hours:
        return 0
    used = (done_at - set_at) / timedelta(hours=window_hours)
    return next((xp for share, xp in EARLY_BONUS if used <= share), 0)


def due_at(hw: Homework) -> datetime | None:
    return hw.created_at + timedelta(hours=hw.completion_window_hours) if hw.completion_window_hours else None


def _step(kind: str, label: str, status: str, **extra) -> dict:
    return {"kind": kind, "label": label, "status": status, "due_at": None, "target_id": None,
            "note": None, **extra}


def _homework_step(hw: Homework | None, progress: StudentHomeworkProgress | None, *, waiting: bool,
                   now: datetime) -> dict:
    if hw is None or progress is None:
        return _step("homework", "Homework", "waiting" if waiting else "locked",
                     note="Being made for you" if waiting else None)
    if progress.status == StudentHomeworkStatus.COMPLETED:
        bonus = early_bonus_xp(hw.created_at, hw.completion_window_hours, progress.confirmed_at or now)
        return _step("homework", "Homework", "done", target_id=str(hw.id),
                     note=f"Done early · +{bonus} XP" if bonus else None)
    due = due_at(hw)
    return _step("homework", "Homework", "current", target_id=str(hw.id), due_at=due,
                 note="Overdue - finish it now" if due and due < now else None)


def _chain_state(steps: list[dict]) -> str:
    return "done" if all(s["status"] in ("done", "skipped") for s in steps) else "open"


# ------------------------------------------------------------------ topics
def _topic_task(loop, hw, progress, now) -> dict:
    stage, quiz, recheck = loop.stage, loop.quiz, loop.recheck
    quiz_step = _step("quiz", "Quiz", "current" if stage == "quiz" else "done", target_id=str(loop.topic.id),
                      note=f"{quiz.score_percent}%" if quiz and quiz.outcome else None)
    if stage == "quiz":
        hw_step, re_step = _step("homework", "Homework", "locked"), _step("recheck", "Re-check", "locked")
    elif quiz.outcome == "mastered":
        hw_step = _step("homework", "Homework", "skipped", note="Not needed")
        re_step = _step("recheck", "Re-check", "skipped")  # the task says "Mastered"
    else:
        hw_step = _homework_step(hw, progress, waiting=stage == "homework_pending", now=now)
        if (quiz.analysis or {}).get("homework_skipped"):
            hw_step = _step("homework", "Homework", "skipped", note="Not needed")
        if recheck is not None and recheck.outcome is not None:
            re_step = _step("recheck", "Re-check", "done", target_id=str(loop.topic.id),
                            note=f"{recheck.score_percent}%")
        else:
            re_step = _step("recheck", "Re-check", "current" if stage == "recheck" else "locked",
                            target_id=str(loop.topic.id))
    steps = [quiz_step, hw_step, re_step]
    result = {"mastered": "Mastered", "needs_teacher": "Your teacher will help"}.get(stage)
    return {"id": f"topic:{loop.topic.id}", "kind": "topic", "title": daily_practice.tidy(loop.topic.name),
            "subtitle": "Topic quiz", "state": _chain_state(steps), "result": result, "steps": steps,
            "sort_at": loop.taught_on}


# ------------------------------------------------------------------ voice tests
async def _test_tasks(session: AsyncSession, student: User, now: datetime) -> list[dict]:
    tests = (await session.execute(
        select(VoiceTest).join(VoiceTestTargetStudent, VoiceTestTargetStudent.test_id == VoiceTest.id)
        .where(VoiceTestTargetStudent.student_id == student.id, VoiceTest.status != TestStatus.DRAFT)
    )).scalars().all()
    if not tests:
        return []
    taken = set((await session.execute(taken_test_ids(student.id))).scalars().all())
    tests = [t for t in tests if t.id in taken or (t.due_at is None or t.due_at > now)]  # missed = gone
    homework = {hw.test_id: (hw, progress) for hw, progress in (await session.execute(
        select(Homework, StudentHomeworkProgress)
        .join(HomeworkTargetStudent, HomeworkTargetStudent.homework_id == Homework.id)
        .join(StudentHomeworkProgress, (StudentHomeworkProgress.homework_id == Homework.id)
              & (StudentHomeworkProgress.student_id == student.id))
        .where(HomeworkTargetStudent.student_id == student.id, Homework.test_id.in_([t.id for t in tests]))
        .order_by(Homework.created_at))).all()}
    checks = {m.voice_test_id: m for m in await mcq_test.sync_for_student(session, student.id)}
    out = []
    for test in tests:
        chapter = await session.get(Chapter, test.chapter_id)
        subject = await session.get(Subject, test.subject_id)
        done = test.id in taken
        hw, progress = homework.get(test.id, (None, None))
        voice = _step("voice_test", "Voice test", "done" if done else "current", target_id=str(test.id),
                      due_at=None if done else test.due_at)
        hw_step = (_homework_step(hw, progress, waiting=True, now=now) if done
                   else _step("homework", "Homework", "locked"))
        check = checks.get(test.id)
        if check is None or not done:
            check_step = _step("mcq_check", "MCQ check", "locked")
        else:
            status = {"completed": "done", "available": "current", "locked": "waiting"}[mcq_test.status(check)]
            check_step = _step("mcq_check", "MCQ check", status, target_id=str(check.id),
                               note=f"{check.score_percent}%" if status == "done" else
                               "Opens after your homework" if status == "waiting" else None)
            if status == "waiting" and hw_step["status"] == "done":
                check_step["note"] = "Opening soon"
        steps = [voice, hw_step, check_step]
        out.append({"id": f"test:{test.id}", "kind": "test",
                    "title": chapter.name if chapter else "Voice test",
                    "subtitle": f"{subject.name if subject else ''} · Co-learner test".strip(" ·"),
                    "state": _chain_state(steps), "result": None, "steps": steps,
                    "sort_at": test.scheduled_at or test.created_at})
    return out


async def tasks_for(session: AsyncSession, student: User) -> list[dict]:
    """Open tasks first (the soonest deadline first, then the newest), then
    the last DONE_SHOWN finished ones."""
    now = datetime.now(timezone.utc)
    loops = await topic_quiz.loop_of(session, student)
    hw_ids = [lp.quiz.homework_id for lp in loops if lp.quiz and lp.quiz.homework_id]
    rows = {hw.id: (hw, progress) for hw, progress in (await session.execute(
        select(Homework, StudentHomeworkProgress)
        .join(StudentHomeworkProgress, (StudentHomeworkProgress.homework_id == Homework.id)
              & (StudentHomeworkProgress.student_id == student.id))
        .where(Homework.id.in_(hw_ids)))).all()} if hw_ids else {}
    tasks = [_topic_task(lp, *rows.get(lp.quiz.homework_id if lp.quiz else None, (None, None)), now)
             for lp in loops]
    tasks += await _test_tasks(session, student, now)

    def stamp(value) -> float:
        if value is None:
            return 0.0
        if not isinstance(value, datetime):  # a taught_on date
            value = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return value.timestamp()

    def deadline(task) -> float:
        dues = [s["due_at"] for s in task["steps"] if s["status"] == "current" and s["due_at"]]
        return min(stamp(d) for d in dues) if dues else float("inf")

    open_ = sorted((t for t in tasks if t["state"] == "open"), key=lambda t: (deadline(t), -stamp(t["sort_at"])))
    done = sorted((t for t in tasks if t["state"] == "done"), key=lambda t: -stamp(t["sort_at"]))[:DONE_SHOWN]
    for t in open_ + done:
        t.pop("sort_at")
    return open_ + done

