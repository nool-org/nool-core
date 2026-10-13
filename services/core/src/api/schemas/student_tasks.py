from datetime import datetime
from typing import Literal

from src.api.schemas.common import CamelModel


class TaskStepOut(CamelModel):
    kind: Literal["quiz", "homework", "recheck", "voice_test", "mcq_check", "test"]
    label: str
    status: Literal["done", "current", "waiting", "locked", "skipped"]
    due_at: datetime | None
    target_id: str | None  # what the step opens: topic, homework, voice test, MCQ check or written test id
    note: str | None


class TaskOut(CamelModel):
    id: str
    kind: Literal["topic", "test", "quiz_test"]
    title: str
    subtitle: str
    subject: str | None = None  # the subject's name - the app filters and colours by it
    state: Literal["open", "done"]
    result: str | None
    steps: list[TaskStepOut]


class TasksOut(CamelModel):
    tasks: list[TaskOut]
