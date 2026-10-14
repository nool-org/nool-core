from .common import CamelModel


class LeaderboardEntryOut(CamelModel):
    student_id: str
    display_name: str
    initials: str
    avatar: str | None = None  # the picture they picked, else initials
    points: int
    rank: int
    is_current_student: bool


class ClassLeaderboardOut(CamelModel):
    class_label: str
    entries: list[LeaderboardEntryOut]
