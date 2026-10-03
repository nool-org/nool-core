from datetime import datetime

from src.domain.models import (
    BloomLevel,
    HomeworkStatus,
    QuestionPaperStatus,
    TestStatus,
)

from .common import CamelModel


class SchoolVoiceTestOut(CamelModel):
    """A read-only, school-wide row for School Admin oversight - the
    teacher name is a best-effort resolution via TeacherClassAssignment
    (the class+subject pair), not a stored column, since VoiceTest itself
    doesn't record who created it.
    """

    id: str
    class_label: str
    subject_name: str
    teacher_name: str | None
    status: TestStatus
    assigned_count: int
    completed_count: int
    created_at: datetime


class SchoolHomeworkOut(CamelModel):
    id: str
    class_label: str
    gap_topic: str
    teacher_name: str | None
    status: HomeworkStatus
    assigned_count: int
    completed_count: int


class SchoolQuestionPaperOut(CamelModel):
    id: str
    name: str
    exam_type: str
    subject_id: str
    subject_name: str
    grade: int
    created_by_name: str
    status: QuestionPaperStatus
    created_at: datetime


class SchoolLeaderboardEntryOut(CamelModel):
    student_id: str
    display_name: str
    class_label: str
    points: int
    rank: int


class SchoolQuestionBankEntryOut(CamelModel):
    """One previously-generated question, pulled from wherever it was
    actually generated (a Question Paper or a Homework set) - there's no
    separate "question bank" table; this is a read-time aggregation over
    QuestionPaperQuestion and HomeworkQuestion so nothing already
    generated at this school is ever re-typed from scratch. topic_label
    is the best topic-shaped label available for that source: the paper's
    linked Topics (falling back to its Subject) for Question Paper
    questions, or the Homework's own gap_topic for Homework questions -
    neither table tracks a topic on the individual question row itself.
    """

    id: str
    text: str
    answer: str | None
    bloom_level: BloomLevel
    subject_name: str
    topic_label: str
    source: str
    source_name: str
    created_at: datetime | None
    # Only meaningful for source="CUSTOM" - None for QUESTION_PAPER/
    # HOMEWORK rows, which have no such concept. See
    # CustomQuestion.collection_name's docstring.
    collection_name: str | None = None
    # CUSTOM rows only: the school-owned bank (dataset) the question is in.
    dataset_id: str | None = None


class SchoolAuditLogEntryOut(CamelModel):
    """School-scoped counterpart to Super Admin's GET /admin/audit-log -
    only entries whose actor belongs to this caller's own school (an
    action a Super Admin took on this school, e.g. creating its
    subscription, is deliberately out of scope here - that's visible on
    the Super Admin side, not this one)."""

    id: str
    actor_name: str
    action: str
    target_type: str
    target_id: str
    detail: str | None
    created_at: datetime
