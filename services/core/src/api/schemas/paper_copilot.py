"""Paper Copilot (C12) and the C11 edit ops it proposes. The rules check is
W5-01's C10 shape, passed through as a plain dict."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_serializer

from src.domain.models import BloomLevel, PaperDifficultyLevel, QuestionType

from .common import CamelModel

QuickAction = Literal["harder", "easier", "more_application", "balance_chapters", "simplify_wording", "fix_total_marks"]


class NewQuestion(CamelModel):
    text: str
    question_type: QuestionType
    marks: int = Field(ge=1)
    bloom_level: BloomLevel
    difficulty: PaperDifficultyLevel
    chapter_id: str
    topic_id: str | None = None
    options: list[str] | None = None
    answer: str | None = None
    source: Literal["bank", "ai"]
    dataset_question_id: str | None = None


class QuestionChanges(CamelModel):
    text: str | None = None
    marks: int | None = Field(default=None, ge=1)
    options: list[str] | None = None
    answer: str | None = None
    bloom_level: BloomLevel | None = None
    difficulty: PaperDifficultyLevel | None = None
    question_type: QuestionType | None = None


class ReplaceOp(CamelModel):
    op: Literal["replace"]
    question_id: str
    new_question: NewQuestion


class AddOp(CamelModel):
    op: Literal["add"]
    section_name: str | None = None
    position: int | None = Field(default=None, ge=1)
    new_question: NewQuestion


class RemoveOp(CamelModel):
    op: Literal["remove"]
    question_id: str


class EditOp(CamelModel):
    op: Literal["edit"]
    question_id: str
    changes: QuestionChanges

    @field_serializer("changes")
    def _only_set_changes(self, changes: QuestionChanges) -> dict:
        # "text?" etc. - a key present means "change it"
        return changes.model_dump(mode="json", by_alias=True, exclude_none=True)


class ReorderOp(CamelModel):
    op: Literal["reorder"]
    ordered_question_ids: list[str]


PaperOp = Annotated[ReplaceOp | AddOp | RemoveOp | EditOp | ReorderOp, Field(discriminator="op")]


class QuestionSummary(CamelModel):
    question_id: str
    text: str
    marks: int
    bloom_level: BloomLevel
    difficulty: PaperDifficultyLevel | None


class AfterSummary(CamelModel):
    text: str
    marks: int
    bloom_level: BloomLevel
    difficulty: PaperDifficultyLevel | None
    source: Literal["bank", "ai", "edited"]


class Proposal(CamelModel):
    proposal_id: str
    op: PaperOp
    before: QuestionSummary | None
    after: AfterSummary | None
    reason: str


class PaperChatIn(CamelModel):
    message: str | None = Field(default=None, max_length=2000)
    audio_base64: str | None = None
    quick_action: QuickAction | None = None
    mime_type: str | None = None  # of the audio; m4a when omitted


class PaperChatOut(CamelModel):
    transcript: str | None
    reply: str
    proposals: list[Proposal]
    preview_rules_check: dict[str, Any] | None


class PaperChatMessageOut(CamelModel):
    id: str
    role: Literal["teacher", "copilot"]
    text: str
    proposals: list[dict[str, Any]] | None
    created_at: datetime


class PaperChatHistoryOut(CamelModel):
    messages: list[PaperChatMessageOut]


class ExplainOut(CamelModel):
    why: str
    bloom_level: BloomLevel
    chapter_name: str | None
    difficulty: PaperDifficultyLevel | None
    alternative: Proposal | None
