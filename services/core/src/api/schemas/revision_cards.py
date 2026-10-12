from typing import Literal

from src.api.schemas.common import CamelModel

DeckStatus = Literal["ready", "generating", "failed"]


class RevisionCardOut(CamelModel):
    kind: Literal["concept", "definition", "formula", "mistake", "check"]
    title: str
    body: str
    answer: str | None = None


class TopicRevisionCardsOut(CamelModel):
    topic_id: str
    topic_name: str
    chapter_name: str
    status: DeckStatus
    cards: list[RevisionCardOut]


class ChapterRevisionTopicOut(CamelModel):
    topic_id: str
    topic_name: str
    status: DeckStatus | None  # null: never opened yet - generated on first open
    card_count: int


class ChapterRevisionCardsOut(CamelModel):
    chapter_name: str
    topics: list[ChapterRevisionTopicOut]
