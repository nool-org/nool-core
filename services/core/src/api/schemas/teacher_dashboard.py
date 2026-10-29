from src.domain.models import TestStatus

from .common import CamelModel


class TeacherDashboardClassOption(CamelModel):
    id: str
    label: str
    meta: str


class TeacherHeadlineTest(CamelModel):
    id: str
    title: str
    meta: str
    status: TestStatus


class TeacherClassStats(CamelModel):
    average_percent: int  # mean of each student's latest test mastery
    week_delta: int  # that average now minus a week ago
    need_help: int  # students whose latest mastery is under 50%


class WeakestTopicOut(CamelModel):
    name: str
    percent: int
    trend: list[int]  # the topic's class average on each of the last 5 tests that scored it, oldest first


class TeacherDashboardOut(CamelModel):
    class_options: list[TeacherDashboardClassOption]
    active_class: TeacherDashboardClassOption
    headline_test: TeacherHeadlineTest | None  # null when the class has no Test yet
    stats: TeacherClassStats | None = None  # null until the class has a result
    weakest_topic: WeakestTopicOut | None = None
