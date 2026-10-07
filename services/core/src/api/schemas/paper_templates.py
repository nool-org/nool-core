from datetime import datetime

from .common import CamelModel


class CreatePaperTemplateIn(CamelModel):
    from_paper_id: str
    name: str


class PaperTemplateOut(CamelModel):
    id: str
    name: str
    exam_type: str
    board: str
    grade: int
    subject_id: str
    total_marks: int | None
    duration_minutes: int | None
    created_by: str
    created_at: datetime


class UsePaperTemplateIn(CamelModel):
    name: str
    chapter_ids: list[str] | None = None
    topic_ids: list[str] | None = None


class UsePaperTemplateOut(CamelModel):
    paper_id: str
