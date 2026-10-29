"""T-011: Today's class average, '+N this week', need help and weakest topic."""
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.api.routes import teacher_dashboard
from src.domain.models import AssignmentTargetMode, StudentTestResult, StudentTestResultTopicScore, TestStatus, VoiceTest
from tests.test_journey_e2e import _world


async def _add_created_at(db):
    path = Path(__file__).resolve().parents[3] / "database/migrations/versions/e3c5d7f9a1b2_student_test_result_created_at.py"
    spec = importlib.util.spec_from_file_location("t011_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def work(conn):
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    await (await db.connection()).run_sync(work)


@pytest.mark.asyncio
async def test_today_stats_and_weakest_topic(db_session, monkeypatch):
    db = db_session
    w = await _world(db, monkeypatch)
    await _add_created_at(db)
    now = datetime.now(timezone.utc)
    first, second = (VoiceTest(class_id=w.cls.id, subject_id=w.subject.id, chapter_id=chapter, duration_minutes=10,
                               completion_window_hours=48, target_mode=AssignmentTargetMode.WHOLE_CLASS,
                               status=TestStatus.COMPLETED, created_at=now - timedelta(days=days))
                     for chapter, days in ((w.ch1.id, 10), (w.ch2.id, 0)))
    db.add_all([first, second])
    await db.flush()
    a, b, c = (s.id for s in w.students)
    # (test, student, mastery, when, topic 12.1 %): latest a 80, b 40, c 30; a week ago a 40, b 40
    for test_id, student, mastery, days_ago, t1 in [(first.id, a, 40, 9, 70), (first.id, b, 40, 8, 30),
                                                    (first.id, c, 30, 1, 20), (second.id, a, 80, 0, 90)]:
        result = StudentTestResult(test_id=test_id, student_id=student, mastery_percent=mastery,
                                   created_at=now - timedelta(days=days_ago))
        db.add(result)
        await db.flush()
        db.add_all([StudentTestResultTopicScore(student_test_result_id=result.id, topic_id=w.t1.id, percent=t1,
                                                answered=1),
                    StudentTestResultTopicScore(student_test_result_id=result.id, topic_id=w.t2.id, percent=95,
                                                answered=1)])
    await db.flush()

    dash = await teacher_dashboard.get_teacher_dashboard(class_id=None, user=w.teacher, session=db)
    assert (dash.stats.average_percent, dash.stats.week_delta, dash.stats.need_help) == (50, 10, 2)
    assert (dash.weakest_topic.percent, dash.weakest_topic.trend) == (52, [40, 90])
    assert dash.weakest_topic.name.startswith("12.1")
