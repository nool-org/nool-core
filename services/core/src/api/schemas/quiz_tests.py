import datetime as dt
from typing import Literal

from pydantic import Field

from src.api.schemas.common import CamelModel


class QuizTestCreateIn(CamelModel):
    chapter_id: str
    topic_ids: list[str] = Field(min_length=1, max_length=12)
    # how many of each: mcq, who_is_right, fix_steps, make_your_own
    mix: dict[str, int]
    title: str | None = Field(default=None, max_length=120)
    due_at: dt.datetime | None = None
    # the same options as a voice co-learner: hours to finish once it opens, and who gets it
    completion_window_hours: int | None = Field(default=None, ge=1, le=168)
    student_ids: list[str] | None = Field(default=None, max_length=200)  # None = the whole class


class QuizTestScheduleIn(CamelModel):
    # None = open it now; a time = it opens to the class then
    opens_at: dt.datetime | None = None


class QuizTestQuestionOut(CamelModel):
    id: str
    kind: str
    bloom_level: str
    topic_id: str | None
    text: str
    options: list[str] | None
    correct_index: int | None  # the teacher sees answers
    spec: dict | None  # full spec, answers included
    explanation: str | None


class QuizTestOut(CamelModel):
    id: str
    class_id: str
    subject_id: str
    chapter_id: str
    title: str
    topic_ids: list[str]
    mix: dict[str, int]
    status: Literal["preparing", "ready", "failed", "scheduled", "published"]
    due_at: dt.datetime | None
    opens_at: dt.datetime | None
    created_at: dt.datetime
    question_count: int
    completion_window_hours: int | None = None
    student_ids: list[str] | None = None
    # how far the class has got - students who finished it / students in the class
    done_count: int = 0
    student_count: int = 0


class QuizTestDetailOut(QuizTestOut):
    questions: list[QuizTestQuestionOut]


class QuizTestStudentResultOut(CamelModel):
    student_id: str
    name: str
    status: Literal["not_started", "in_progress", "done"]
    score_percent: int | None
    per_bloom: dict[str, int] | None
    lagging: list[str]


class QuizTestResultsOut(CamelModel):
    student_count: int
    done: int
    average_percent: int | None
    per_bloom: dict[str, int]
    weakest_level: str | None
    needs_help: list[str]
    students: list[QuizTestStudentResultOut]


class QuizTestListOut(CamelModel):
    items: list[QuizTestOut]
