"""Blueprint <-> template JSON. The snapshot is the paper's own output
filtered to the create-body fields, so whatever the blueprint grows (C9's
bloomTargets/difficultyMix/...) is carried without touching this file.

A designed template (C15) is stored in the same shape - sections as
{label, question_count, marks_each} plus their own question_types - so
using either kind is the same CreateQuestionPaperIn."""

from pydantic import ValidationError as PydanticValidationError
from pydantic.alias_generators import to_camel

from shared.errors import ValidationError
from shared.errors.handlers import validation_message

from src.api.schemas.paper_templates import TemplateBlueprint
from src.api.schemas.question_paper import CreateQuestionPaperIn, QuestionPaperOut
from src.domain.models import QuestionType

# QuestionType's values plus the CBSE labels the app's blueprint offers
# (nool-apps features/question-paper/questionTypePresets.ts).
QUESTION_TYPE_VALUES = (
    *(t.value for t in QuestionType),
    "Assertion-Reason", "Very Short Answer", "Short Answer I", "Short Answer II", "Long Answer",
    "Case-Based", "Competency-Based",
)


def snapshot_blueprint(paper: QuestionPaperOut) -> dict:
    fields = CreateQuestionPaperIn.model_fields.keys() - {"name"}
    return {k: v for k, v in paper.model_dump(mode="json").items() if k in fields}


def blueprint_view(blueprint: dict) -> dict:
    """The stored JSON as TemplateBlueprint fields (snake_case). A paper
    snapshot's sections have no per-section types; its older
    bloom_distribution/difficulty_distribution rows stand in for the C9 shape."""
    bloom = blueprint.get("bloom_targets") or [
        {"level": d["level"], "percent": d["value"]} for d in blueprint.get("bloom_distribution") or []]
    mix = blueprint.get("difficulty_mix")
    if mix is None and blueprint.get("difficulty_distribution"):
        mix = {d["level"].lower(): d["value"] for d in blueprint["difficulty_distribution"]}
    return {
        "exam_type": blueprint.get("exam_type"), "board": blueprint.get("board"), "grade": blueprint.get("grade"),
        "subject_id": blueprint.get("subject_id"), "language": blueprint.get("language") or "English",
        "total_marks": blueprint.get("total_marks"), "duration_minutes": blueprint.get("duration_minutes"),
        "sections": [{"name": s["label"], "question_count": s["question_count"], "marks_each": s["marks_each"],
                      "question_types": s.get("question_types") or []} for s in blueprint.get("sections") or []],
        "bloom_targets": bloom, "difficulty_mix": mix, "chapter_ids": blueprint.get("chapter_ids") or [],
    }


def validate_blueprint(data: dict) -> TemplateBlueprint:
    """C15's rules, each a 422 with a message the teacher can act on."""
    try:
        # by alias, so a message names the field as the client sent it (examType)
        bp = TemplateBlueprint.model_validate({to_camel(k): v for k, v in data.items()})
    except PydanticValidationError as exc:
        raise ValidationError(validation_message(exc.errors())) from None
    if not 1 <= bp.grade <= 12:
        raise ValidationError("grade must be between 1 and 12.")
    if not bp.exam_type.strip() or not bp.board.strip():
        raise ValidationError("examType and board are required.")
    if not bp.sections:
        raise ValidationError("A template needs at least one section.")
    for s in bp.sections:
        if not s.name.strip() or s.question_count < 1 or s.marks_each < 1:
            raise ValidationError("Every section needs a name, at least one question and at least 1 mark each.")
        unknown = [t for t in s.question_types if t not in QUESTION_TYPE_VALUES]
        if unknown:
            raise ValidationError(f'Unknown question type "{unknown[0]}" in {s.name}. '
                                  f"Use one of: {', '.join(QUESTION_TYPE_VALUES)}.")
    section_marks = sum(s.question_count * s.marks_each for s in bp.sections)
    if section_marks != bp.total_marks:
        raise ValidationError(f"The sections add up to {section_marks} marks but totalMarks is {bp.total_marks}.")
    if bp.duration_minutes is not None and bp.duration_minutes < 1:
        raise ValidationError("durationMinutes must be at least 1.")
    if bp.bloom_targets and sum(t.percent for t in bp.bloom_targets) != 100:
        raise ValidationError("bloomTargets percents must add up to 100.")
    mix = bp.difficulty_mix
    if mix is not None and mix.easy + mix.medium + mix.hard != 100:
        raise ValidationError("difficultyMix must add up to 100.")
    return bp


def blueprint_json(bp: TemplateBlueprint, previous: dict | None = None) -> dict:
    """The validated blueprint as stored JSON, keeping a snapshot's extra
    keys (instructions, dataset shares, ...) unless they no longer fit."""
    data = dict(previous or {})
    data.pop("bloom_distribution", None)
    data.pop("difficulty_distribution", None)
    if data.get("subject_id") != bp.subject_id:  # shares/topics are the old subject's
        data.pop("dataset_shares", None)
        data.pop("topic_ids", None)
    if (data.get("chapter_ids") or []) != (bp.chapter_ids or []):
        data.pop("topic_ids", None)
    types = list(dict.fromkeys(t for s in bp.sections for t in s.question_types))
    data.update(
        exam_type=bp.exam_type, board=bp.board, grade=bp.grade, language=bp.language, subject_id=bp.subject_id,
        total_marks=bp.total_marks, duration_minutes=bp.duration_minutes,
        sections=[{"label": s.name, "question_count": s.question_count, "marks_each": s.marks_each,
                   "question_types": s.question_types} for s in bp.sections],
        question_types=types or data.get("question_types"),
        bloom_targets=[t.model_dump(mode="json") for t in bp.bloom_targets] if bp.bloom_targets else None,
        difficulty_mix=bp.difficulty_mix.model_dump() if bp.difficulty_mix else None,
        chapter_ids=bp.chapter_ids or None,
    )
    return data


def blueprint_body(blueprint: dict, name: str, chapter_ids: list[str] | None,
                   topic_ids: list[str] | None, subject_id: str | None = None) -> CreateQuestionPaperIn:
    data = {**blueprint, "name": name}
    if subject_id is not None and subject_id != data.get("subject_id"):
        # chapters, topics and question banks belong to the template's subject
        data.update(subject_id=subject_id, chapter_ids=None, topic_ids=None, dataset_shares=None)
    if not data.get("subject_id"):
        raise ValidationError("This template has no subject - pick one (subjectId) to use it.")
    if chapter_ids is not None:
        # the template's topics sit under its chapters, so new chapters drop them
        data["chapter_ids"], data["topic_ids"] = chapter_ids, topic_ids
    elif topic_ids is not None:
        data["topic_ids"] = topic_ids
    return CreateQuestionPaperIn.model_validate(data)
