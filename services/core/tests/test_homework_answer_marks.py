"""T-012: a bank's marking-scheme marks ("(1.0)") stay out of the homework answer."""

from src.services.homework_builder import _with_solution


def test_bullet_marks_are_dropped_but_maths_is_kept():
    solution = "- Identification of the process as a chemical reaction. (0.5)\n- Reasoning on the change (1.0)\nP = (1.2)^2 * 2"
    assert _with_solution("A chemical change.", solution) == (
        "A chemical change.\n\n- Identification of the process as a chemical reaction.\n"
        "- Reasoning on the change\nP = (1.2)^2 * 2")


def test_a_solution_that_repeats_the_answer_is_not_added():
    assert _with_solution("x", " x ") == "x"
    assert _with_solution("x", None) == "x"
