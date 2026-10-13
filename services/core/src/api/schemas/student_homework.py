from datetime import datetime

from .common import CamelModel


class StudentHomeworkOverviewOut(CamelModel):
    homework_id: str
    gap_topic: str
    total_questions: int
    qa_completed: bool  # only flips via POST .../confirm-completion, never local progress alone
    expired: bool


class CurrentHomeworkResponse(CamelModel):
    homework: StudentHomeworkOverviewOut | None


class HomeworkReference(CamelModel):
    title: str
    subtitle: str


class HomeworkLearningContextOut(CamelModel):
    homework_id: str
    topic_label: str
    what_needs_understanding: str
    references: list[HomeworkReference]
    key_idea_title: str
    key_idea_body: str
    connection_prompt: str
    # for "Revise this topic": the test's chapter, and its topic when topic_label names one
    chapter_id: str | None = None
    topic_id: str | None = None


class HomeworkQaConfirmationOut(CamelModel):
    confirmed_at: datetime
    # the MCQ check this homework unlocks (its questions are being built) - the app opens it next
    mcq_test_id: str | None = None
    recheck_topic_id: str | None = None  # a topic quiz's homework: its re-check quiz is open now
    recheck_test_id: str | None = None  # a teacher's challenge's homework: its second look is open now
    early_bonus_xp: int = 0  # for finishing before the window ran out (student_tasks.EARLY_BONUS)
