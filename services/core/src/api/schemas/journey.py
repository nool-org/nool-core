from .common import CamelModel


class JourneyChapterNodeOut(CamelModel):
    chapter_id: str
    chapter_label: str
    state: str  # "DONE" | "CURRENT" | "LOCKED"
    stars_earned: int
    topic_count: int = 0  # 0 = the chapter's topics aren't loaded yet (nothing to revise)


class SubjectJourneyOut(CamelModel):
    subject_id: str
    subject_label: str
    nodes: list[JourneyChapterNodeOut]
