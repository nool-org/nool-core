from datetime import datetime

from src.domain.models import ChatRole

from .common import CamelModel


class ChatMessageOut(CamelModel):
    id: str
    role: ChatRole
    text: str
    created_at: datetime


class SendMessageIn(CamelModel):
    text: str
    # What the teacher picked to talk about (the app asks first): a class, and
    # within it one challenge - a Speak with Milo test or a Fix Milo challenge
    focus_label: str | None = None  # e.g. "Class 10A · Electricity", shown to the model
    test_id: str | None = None
    quiz_test_id: str | None = None


class TeacherSummaryOut(CamelModel):
    bullets: list[str]
