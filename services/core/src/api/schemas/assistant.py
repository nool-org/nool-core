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


class TeacherSummaryOut(CamelModel):
    bullets: list[str]
