from datetime import datetime
from typing import Literal

from .common import CamelModel
from .question_paper import BloomTargetPercent, DifficultyMix

TemplateScope = Literal["personal", "school", "starter"]


class TemplateSection(CamelModel):
    name: str
    question_count: int
    marks_each: int
    question_types: list[str] = []


class TemplateBlueprint(CamelModel):
    """A template's complete blueprint (C15) - what a designed template is
    built from and what every template is shown as."""

    exam_type: str
    board: str
    grade: int
    subject_id: str | None = None
    language: str = "English"
    total_marks: int
    duration_minutes: int | None = None
    sections: list[TemplateSection]
    bloom_targets: list[BloomTargetPercent] | None = None
    difficulty_mix: DifficultyMix | None = None
    chapter_ids: list[str] | None = None


class PaperTemplateFieldsIn(CamelModel):
    """Every C15 field, optional: PATCH sends any of them, a designed create
    sends the full blueprint (checked against TemplateBlueprint)."""

    name: str | None = None
    scope: Literal["personal", "school"] | None = None
    exam_type: str | None = None
    board: str | None = None
    grade: int | None = None
    subject_id: str | None = None
    language: str | None = None
    total_marks: int | None = None
    duration_minutes: int | None = None
    sections: list[TemplateSection] | None = None
    bloom_targets: list[BloomTargetPercent] | None = None
    difficulty_mix: DifficultyMix | None = None
    chapter_ids: list[str] | None = None


class CreatePaperTemplateIn(PaperTemplateFieldsIn):
    """Either {fromPaperId, name} (snapshot a paper) or a designed
    {name, scope, ...blueprint}."""

    name: str
    from_paper_id: str | None = None


class UpdatePaperTemplateIn(PaperTemplateFieldsIn):
    pass


class DuplicatePaperTemplateIn(CamelModel):
    name: str | None = None


class PaperTemplateOut(CamelModel):
    id: str
    name: str
    scope: TemplateScope
    exam_type: str
    board: str
    grade: int
    subject_id: str | None
    language: str
    total_marks: int | None
    duration_minutes: int | None
    sections: list[TemplateSection]
    bloom_targets: list[BloomTargetPercent]
    difficulty_mix: DifficultyMix | None
    chapter_ids: list[str]
    created_by: str | None
    created_at: datetime | None
    can_edit: bool = False
    can_delete: bool = False  # the caller created it, or is a school admin (school scope)
    is_starter: bool = False


class UsePaperTemplateIn(CamelModel):
    name: str
    subject_id: str | None = None  # required when the template has none (starters)
    chapter_ids: list[str] | None = None
    topic_ids: list[str] | None = None


class UsePaperTemplateOut(CamelModel):
    paper_id: str
