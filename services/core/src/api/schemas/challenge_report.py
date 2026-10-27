from typing import Literal

from .common import CamelModel


class ReportTopicOut(CamelModel):
    topic_id: str
    name: str
    page: int | None
    percent: int
    students: int
    struggling: list[str]


class ReportSkillOut(CamelModel):
    level: str
    name: str
    percent: int | None


class ReportQuestionOut(CamelModel):
    text: str
    level: str
    percent: int
    answered: int
    hints: int
    notes: list[str]  # misconceptions / the wrong answers picked


class ReportStudentOut(CamelModel):
    student_id: str
    name: str
    status: Literal["not_started", "in_progress", "done"]
    score: int | None
    weakest_topic: str | None
    weakest_skill: str | None
    hints_used: int
    homework: Literal["none", "pending", "done"]
    recheck_score: int | None
    # topic id -> this student's percent on it (the students-by-topic heatmap)
    topic_scores: dict[str, int] = {}


class ChallengeReportOut(CamelModel):
    kind: Literal["speak", "fix"]
    title: str
    class_label: str
    chapter: str
    assigned: int
    finished: int
    in_progress: int
    not_started: list[str]
    average_percent: int | None
    topics: list[ReportTopicOut]
    skills: list[ReportSkillOut]
    questions: list[ReportQuestionOut]
    students: list[ReportStudentOut]
    actions: list[str]
