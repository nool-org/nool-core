"""Built-in, read-only starter templates (C15): CBSE Class 9 and 10 unit
test, half-yearly and pre-board patterns. Code constants, no DB rows - ids
are "starter:<slug>". subjectId is null, so using one needs a subject;
duplicating one gives the teacher an editable personal copy."""

from src.api.schemas.paper_templates import TemplateBlueprint, TemplateSection

STARTER_PREFIX = "starter:"


def _section(name: str, count: int, marks: int, *types: str) -> TemplateSection:
    return TemplateSection(name=name, question_count=count, marks_each=marks, question_types=list(types))


def _bloom(remember: int, understand: int, apply: int, analyze: int, evaluate: int, create: int) -> list[dict]:
    levels = ("REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE", "EVALUATE", "CREATE")
    values = (remember, understand, apply, analyze, evaluate, create)
    return [{"level": level, "percent": value} for level, value in zip(levels, values) if value]


# CBSE board pattern, 80 marks: A objective, B very short, C short, D long, E case-based
_CBSE_80 = [
    _section("Section A", 20, 1, "MCQ", "Assertion-Reason"),
    _section("Section B", 5, 2, "Very Short Answer"),
    _section("Section C", 6, 3, "Short Answer II"),
    _section("Section D", 4, 5, "Long Answer"),
    _section("Section E", 3, 4, "Case-Based"),
]
_UNIT_25 = [
    _section("Section A", 5, 1, "MCQ"),
    _section("Section B", 5, 2, "Short Answer I"),
    _section("Section C", 2, 5, "Long Answer"),
]
_PATTERNS = {
    "unit-test": ("Unit test", "Unit Test", 25, 45, _UNIT_25, _bloom(25, 35, 30, 10, 0, 0),
                  {"easy": 40, "medium": 45, "hard": 15}),
    "half-yearly": ("Half-yearly", "Half-Yearly", 80, 180, _CBSE_80, _bloom(20, 30, 30, 15, 5, 0),
                    {"easy": 30, "medium": 50, "hard": 20}),
    "pre-board": ("Pre-board", "Pre-Board", 80, 180, _CBSE_80, _bloom(15, 25, 30, 20, 10, 0),
                  {"easy": 25, "medium": 50, "hard": 25}),
}


def _build() -> dict[str, tuple[str, TemplateBlueprint]]:
    starters = {}
    for grade in (10, 9):
        for slug, (name, exam_type, marks, minutes, sections, bloom, mix) in _PATTERNS.items():
            starters[f"{STARTER_PREFIX}cbse-{grade}-{slug}"] = (f"CBSE Class {grade} {name}", TemplateBlueprint(
                exam_type=exam_type, board="CBSE", grade=grade, subject_id=None, language="English",
                total_marks=marks, duration_minutes=minutes, sections=sections, bloom_targets=bloom,
                difficulty_mix=mix, chapter_ids=[],
            ))
    return starters


# id -> (name, blueprint)
STARTERS: dict[str, tuple[str, TemplateBlueprint]] = _build()


def is_starter_id(template_id: str) -> bool:
    return template_id.startswith(STARTER_PREFIX)
