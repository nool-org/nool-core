"""question_paper._plan_slots: the per-question plan sent to the kg generator."""

from collections import Counter

from src.api.routes.question_paper import _kg_question_types, _plan_slots, _question_type
from src.domain.models.custom_question import QuestionType


def _marks_by(slots, key):
    out = Counter()
    for s in slots:
        out[s[key]] += s["marks"]
    return out


def test_bloom_and_difficulty_follow_marks_not_question_count():
    marks = [1] * 5 + [2] * 5 + [5] * 2  # CBSE unit test, 25 marks
    slots = _plan_slots(marks, ["A"] * 5 + ["B"] * 5 + ["C"] * 2,
                        {"REMEMBER": 25, "UNDERSTAND": 35, "APPLY": 30, "ANALYZE": 10},
                        {"EASY": 40, "MEDIUM": 45, "HARD": 15}, ["MCQ", "Short Answer I", "Long Answer"])
    bloom = _marks_by(slots, "bloom_level")
    for level, pct in {"REMEMBER": 25, "UNDERSTAND": 35, "APPLY": 30, "ANALYZE": 10}.items():
        assert abs(bloom[level] - pct * 25 / 100) <= 2, (level, bloom)
    assert "CREATE" not in bloom and "EVALUATE" not in bloom
    diff = _marks_by(slots, "difficulty")
    # T-070: a 4+ mark question is never EASY, so some EASY marks move to small questions (within 5 marks of target)
    assert abs(diff["EASY"] - 10) <= 5 and abs(diff["HARD"] - 3.75) <= 5
    assert [s["question_type"] for s in slots] == ["mcq"] * 5 + ["short_answer"] * 5 + ["long_answer"] * 2
    assert [s["section"] for s in slots][-1] == "C"


def test_defaults_when_the_paper_has_no_targets():
    slots = _plan_slots([3, 3], [None, None], {}, {}, [])
    assert all(s["bloom_level"] and s["difficulty"] for s in slots)


def test_question_type_mapping():
    assert _kg_question_types(["MCQ", "Assertion-Reason", "Short Answer II", "LONG_ANSWER"]) == [
        "mcq", "short_answer", "long_answer"]
    assert _question_type("mcq") is QuestionType.MCQ
    assert _question_type("numerical") is QuestionType.SHORT_ANSWER
    assert _question_type(None) is None


def test_a_sections_own_types_override_the_papers():
    slots = _plan_slots([2, 2], ["A", "B"], {}, {}, ["MCQ", "Short Answer I"],
                        [["Long Answer"], None])
    assert [s["question_type"] for s in slots] == ["long_answer", "short_answer"]
