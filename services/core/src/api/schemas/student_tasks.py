from datetime import datetime
from typing import Literal

from src.api.schemas.common import CamelModel


class TaskStepOut(CamelModel):
    kind: Literal["quiz", "homework", "recheck", "voice_test", "mcq_check", "test", "test_recheck"]
    label: str
    status: Literal["done", "current", "waiting", "locked", "skipped"]
    due_at: datetime | None
    target_id: str | None  # what the step opens: topic, homework, voice test, MCQ check or written test id
    note: str | None


class TaskOut(CamelModel):
    id: str
    kind: Literal["topic", "test", "quiz_test", "homework"]  # homework: set by the teacher on a chapter
    title: str
    subtitle: str
    subject: str | None = None  # the subject's name - the app filters and colours by it
    # where it sits: a topic checkpoint's topic, and the chapter (a co-learner is chapter-wide)
    chapter_id: str | None = None
    chapter_name: str | None = None
    topic_id: str | None = None
    state: Literal["open", "done"]
    result: str | None
    steps: list[TaskStepOut]


class TasksOut(CamelModel):
    tasks: list[TaskOut]
