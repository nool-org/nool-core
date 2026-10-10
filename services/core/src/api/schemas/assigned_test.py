from datetime import datetime

from src.domain.models import BloomLevel

from .common import CamelModel


class AssignedTestOut(CamelModel):
    """The student's own read view of a Voice Test - deliberately a
    different shape from VoiceTestOut: a student never sees class roster
    or draft-authoring fields.
    """

    id: str
    subject_id: str
    subject_label: str
    # Which chapter this Test's win screen hands off to on the Journey map
    # (see journey.py's GET /me/journey) once the student finishes it.
    chapter_id: str
    title: str
    meta: str
    in_progress: bool
    bloom_levels: list[BloomLevel]
    duration_minutes: int
    due_at: datetime | None = None  # deadline; the test leaves the list once it passes
