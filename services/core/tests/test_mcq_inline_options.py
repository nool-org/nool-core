from src.services.mcq_test import strip_inline_options


def test_strips_choices_repeated_in_the_stem():
    stem = "The focal length of the mirror is:\na. -20 cm\nb. -10 cm\nc. -5 cm\nd. 20 cm"
    assert strip_inline_options(stem) == "The focal length of the mirror is:"


def test_leaves_other_multiline_stems_alone():
    stem = "Assertion (A): x.\nReason (R): y."
    assert strip_inline_options(stem) == stem
    assert strip_inline_options("Plain question?") == "Plain question?"
