from types import SimpleNamespace as S

from src.services.paper_instructions import sync_instructions

LINES = [
    "All questions are compulsory.",
    "This question paper is divided into 3 sections — Section A, Section B, Section C.",
    "Section A (MCQ) consists of 6 questions of 1 mark each.",
    "Section B (Short Answer I) consists of 4 questions of 2 marks each.",
    "Section C (Short Answer II) consists of 2 questions of 3 marks each.",
    "Marks for each question are indicated against it.",
]


def test_section_lines_follow_the_current_sections():
    after = sync_instructions(LINES, [S(question_count=6, marks_each=1), S(question_count=1, marks_each=2)])
    assert after == [
        "All questions are compulsory.",
        "This question paper is divided into 2 sections — Section A, Section B.",
        "Section A (MCQ) consists of 6 questions of 1 mark each.",
        "Section B (Short Answer I) consists of 1 question of 2 marks each.",  # edited, singular
        "Marks for each question are indicated against it.",  # Section C is gone
    ]


def test_no_instructions_stay_none():
    assert sync_instructions(None, []) is None


def test_t034_instruction_written_for_6_follows_a_blueprint_edited_to_7():
    """T-034: Section A was bumped 6 -> 7 before the first generate; the printed line says 7."""
    after = sync_instructions(["Section A has 6 questions of 1 mark each."], [S(question_count=7, marks_each=1)])
    assert after == ["Section A has 7 questions of 1 mark each."]
