"""B27: T-070 concept repeats / over-marked Remember / no Hard, C-020 figure note in the rules check, C-021 summary
wording, C-022 smooth graph figure."""

import re
import uuid

import pytest

from src.domain.models import BloomLevel, PaperDifficultyLevel, QuestionPaper, QuestionPaperValidation, QuestionType
from src.domain.models.question_paper import QuestionPaperQuestion
from src.services.paper_rules import rules_check
from src.services.question_selector import _separate_labels, _smooth_curves, concept_full, crowded, over_marked
from src.services.topic_quiz import score_summary
from tests.test_question_paper_generate import _seed_paper_ready_to_generate

SOAP = ["Why does soap form scum with hard water?", "How do detergents differ from soap in hard water?",
        "Micelles in soap trap grease; what is this called?"]
OTHER = [f"Topic {w} is asked here in a unique way number {n}?" for n, w in enumerate(
    "refraction magnet circuit lens acid ohm prism".split())]


def test_a_concept_is_capped_at_two_per_paper_soap_and_detergent_count_as_one():
    texts = [*SOAP, *OTHER]
    assert crowded(texts) == {2: "soap"}
    assert concept_full("Which soap is biodegradable?", SOAP[:2] + OTHER, 10)
    assert not concept_full("Which soap is biodegradable?", SOAP[:1] + OTHER, 10)
    assert crowded(SOAP) == {}  # a short paper is not judged


def test_remember_with_three_marks_is_over_marked_unless_multi_part():
    assert over_marked("List the properties of an image formed by a plane mirror.", 3, BloomLevel.REMEMBER)
    assert not over_marked("(a) State Ohm's law. (b) Write its formula.", 3, BloomLevel.REMEMBER)
    assert not over_marked("List two properties of an image.", 2, BloomLevel.REMEMBER)
    assert not over_marked("Explain why the image is virtual.", 3, BloomLevel.UNDERSTAND)


def test_zero_percent_summary_does_not_praise_the_student():
    assert "None of these were right" in score_summary("Chemical Equations", {"REMEMBER": 0, "UNDERSTAND": 0})
    assert score_summary("Chemical Equations", {"REMEMBER": 100, "UNDERSTAND": 0}) is None


def test_graph_pieces_become_one_smooth_curve_and_overlapping_labels_move_apart():
    pts = [(72.5, 184.0), (97.5, 109.0), (122.5, 59.0), (147.5, 84.0), (172.5, 109.0)]
    lines = "".join(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#2D3748" stroke-width="2"/>'
                    for a, b in zip(pts, pts[1:]))
    out = _smooth_curves(f'<svg>{lines}<line x1="1" y1="1" x2="2" y2="2" stroke="#2D3748" stroke-width="2"/></svg>')
    assert out.count("<path") == 1 and out.count(" C ") == 4 and out.count("<line") == 1
    assert _smooth_curves('<svg><line x1="1" y1="1" x2="9" y2="9" stroke="#2D3748" stroke-width="2"/></svg>').count("<line") == 1
    label = '<text x="{}" y="100" font-family="a" font-size="13">(-3,0)</text>'
    ys = [float(y) for y in re.findall(r'y="([\d.]+)"', _separate_labels(label.format(60) + label.format(70)))]
    assert ys[1] - ys[0] >= 13


@pytest.mark.asyncio
async def test_rules_check_reports_repeated_concept_over_marked_no_hard_and_missing_figures(db_session):
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    paper = await db_session.get(QuestionPaper, uuid.UUID(seeded["paper_id"]))
    for order, text in enumerate([*SOAP, *OTHER], start=1):
        db_session.add(QuestionPaperQuestion(
            paper_id=paper.id, order=order, marks=3 if order == 1 else 1, text=text, question_type=QuestionType.SHORT_ANSWER,
            bloom_level=BloomLevel.REMEMBER, difficulty=PaperDifficultyLevel.MEDIUM))
    db_session.add(QuestionPaperValidation(paper_id=paper.id, coverage_percent=100, marks_accounted_for=0, duplicate_count=0, quality_percent=100, issues=[
        "No question with a figure is in this school's question banks for these chapters. Ask your admin to add a bank that has diagrams."]))
    await db_session.flush()
    codes = {i.code: i.message for i in (await rules_check(db_session, paper)).issues}
    assert "soap" in codes["repeated_concept"] and "Remember question worth 3" in codes["over_marked"]
    assert "no_hard" in codes and "No question with a figure" in codes["no_figure_questions"]


def test_pieces_with_dots_and_labels_between_them_still_join():
    seg = '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="#2D3748" stroke-width="2"/><circle cx="{}" cy="{}" r="2"/>'
    xs = [10, 20, 30, 40, 50]
    body = "".join(seg.format(a, 50 - a, b, 50 - b, b, 50 - b) for a, b in zip(xs, xs[1:]))
    assert _smooth_curves(f"<svg>{body}</svg>").count("<circle") == 4 and "<line" not in _smooth_curves(f"<svg>{body}</svg>")


def test_b28_generic_words_are_never_a_repeated_concept_and_the_cap_holds_in_a_chapter_heavy_paper():
    generic = [f"The student {w} in case number {n} is connected to the human body?" for n, w in enumerate(
        "refraction magnet circuit lens acid ohm prism cell".split())]
    assert crowded(generic) == {}  # student / human / body / connected are stop-words
    mirror = [f"Q{n}: which mirror shows case {n} in {w}?" for n, w in enumerate("refraction magnet circuit lens acid".split())]
    texts = [*mirror[:3], *OTHER[:5]]
    assert crowded(texts) == {2: "mirror"}  # 3 of 8 is not "chapter-wide", the cap holds (was exempt at >3 of a 12+ paper)
    # a word only counts when it is one of the chapters' topic words
    assert crowded(texts, terms={"circuit"}) == {}
    assert concept_full("A mirror question", mirror[:2] + OTHER[:5], 10)  # even while the paper is still short (total given)


def test_b28_topic_is_capped_at_a_quarter_of_the_paper():
    from src.services.question_selector import topic_full
    assert topic_full("earthing", ["earthing"] * 3 + ["x"] * 5, 12)
    assert not topic_full("earthing", ["earthing"] * 2 + ["x"] * 5, 12)
    assert not topic_full("earthing", ["earthing"] * 9, 6)  # a short paper is not judged
