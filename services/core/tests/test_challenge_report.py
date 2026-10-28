"""The Speak with Milo side of the teacher's challenge report (the Fix Milo
side is in test_quiz_tests.py): topics weakest first with who struggles,
skills, the hardest questions with their misconceptions, and next steps."""

from sqlalchemy import select

from src.domain.models import AiAssessorSession, AiAssessorSessionRecord, VoiceTestTargetStudent
from src.services import challenge_report
from tests.test_insights import _seed, _student


async def test_speak_with_milo_report_names_the_weak_topic_students_and_misconceptions(db_session):
    test, student = await _seed(db_session)
    absent = _student()
    db_session.add(absent)
    await db_session.flush()
    db_session.add_all([VoiceTestTargetStudent(test_id=test.id, student_id=student.id),
                        VoiceTestTargetStudent(test_id=test.id, student_id=absent.id)])
    record = (await db_session.execute(
        select(AiAssessorSessionRecord).join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.test_id == test.id))).scalar_one()
    record.report = {"qa_transcripts": [
        {"question_number": 1, "question_asked": "What is V = IR?", "blooms_attribute": "Remembering",
         "score_percent": 90, "hints_used": 0},
        {"question_number": 3, "question_asked": "Why does power grow with current?", "blooms_attribute": "Analyzing",
         "score_percent": 35, "hints_used": 2, "misconception": "Thinks power depends only on voltage."},
    ]}
    await db_session.flush()

    report = await challenge_report.voice_report(db_session, test)

    assert (report["kind"], report["assigned"], report["finished"], report["average_percent"]) == ("speak", 2, 1, 55)
    assert report["not_started"] == ["Student"]  # the one who never opened it
    assert [t["name"] for t in report["topics"]] == ["Electric Power", "Ohm's Law"]  # weakest first
    assert report["topics"][0]["struggling"] == ["Student"]
    assert report["questions"][0]["notes"] == ["Thinks power depends only on voltage."]
    assert report["questions"][0]["hints"] == 2
    done = next(s for s in report["students"] if s["status"] == "done")
    assert (done["weakest_topic"], done["weakest_skill"], done["hints_used"]) == ("Electric Power", "Spot patterns", 2)
    assert any(a.startswith("Re-teach Electric Power") for a in report["actions"])
    # the students-by-topic heatmap: each topic's percent for the student, keyed by topic id
    by_name = {t["topic_id"]: t["name"] for t in report["topics"]}
    assert {by_name[t]: p for t, p in done["topic_scores"].items()} == {
        t["name"]: t["percent"] for t in report["topics"]}  # one student, so theirs = the class's
    assert next(s for s in report["students"] if s["status"] == "not_started")["topic_scores"] == {}


async def test_hardest_questions_use_only_each_students_latest_graded_session(db_session):
    """V-002: a reload left an older session record; the report averaged both (Q1 70% for a 90% answer)."""
    from datetime import datetime, timedelta, timezone

    test, student = await _seed(db_session)
    db_session.add(VoiceTestTargetStudent(test_id=test.id, student_id=student.id))
    record = (await db_session.execute(
        select(AiAssessorSessionRecord).join(AiAssessorSession, AiAssessorSession.id == AiAssessorSessionRecord.session_id)
        .where(AiAssessorSession.test_id == test.id))).scalar_one()
    record.report = {"qa_transcripts": [{"question_number": 1, "question_asked": "Q1", "score_percent": 90}]}
    older = AiAssessorSession(student_id=student.id, test_id=test.id, context_label="x", duration_seconds=60)
    db_session.add(older)
    await db_session.flush()
    db_session.add(AiAssessorSessionRecord(
        session_id=older.id, created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        report={"qa_transcripts": [{"question_number": 1, "question_asked": "Q1", "score_percent": 50}]}))
    await db_session.flush()

    report = await challenge_report.voice_report(db_session, test)

    assert [(q["percent"], q["answered"]) for q in report["questions"]] == [(90, 1)]
