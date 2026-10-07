from datetime import datetime

from src.domain.models import BloomLevel

from .common import CamelModel


class PracticeBankEntryOut(CamelModel):
    id: str
    source_homework_id: str
    subject_id: str | None
    subject_name: str | None = None
    subject_grade: int | None = None
    chapter_id: str | None
    topic_id: str | None
    bloom_level: BloomLevel
    text: str
    answer: str
    archived_at: datetime
