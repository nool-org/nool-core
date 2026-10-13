from .common import CamelModel


class SubjectOut(CamelModel):
    id: str
    name: str
    # Subjects are per grade - "Science" (9) and "Science" (10) share a name,
    # so clients show/filter by this.
    grade: int | None = None
    # false for subjects tested in writing, not by voice (Maths - services/subject_rules.py)
    voice_tests: bool = True


class ChapterOut(CamelModel):
    id: str
    subject_id: str
    name: str
    # Null means "applies to any grade" - see Chapter.grade's docstring.
    grade: int | None = None


class TopicOut(CamelModel):
    id: str
    chapter_id: str
    name: str
