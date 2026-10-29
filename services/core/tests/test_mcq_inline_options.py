from src.services.mcq_test import relabel, strip_inline_options


def test_strips_choices_repeated_in_the_stem():
    stem = "The focal length of the mirror is:\na. -20 cm\nb. -10 cm\nc. -5 cm\nd. 20 cm"
    assert strip_inline_options(stem) == "The focal length of the mirror is:"


def test_leaves_other_multiline_stems_alone():
    stem = "Assertion (A): x.\nReason (R): y."
    assert strip_inline_options(stem) == stem
    assert strip_inline_options("Plain question?") == "Plain question?"


def test_with_its_options_strips_only_the_options_even_across_blank_lines():
    plans = ["(A) Weigh it.", "(B) Say no test is needed.", "(C) Use nitric acid."]
    stem = "Which plan should she choose?\n\n" + "\n\n".join(plans)
    options = ["Use nitric acid.", "Test the gas with a burning splint.", "Say no test is needed.", "Weigh it."]
    assert strip_inline_options(stem, options) == "Which plan should she choose?"
    students = "Who is right?\n\nA. Riya adds water to acid.\n\nB. Arjun adds acid to water."
    assert strip_inline_options(students, ["A, because x", "B, because y", "C", "D"]) == students


def test_relabel_moves_option_letters_with_the_shuffle():
    order = [3, 2, 0, 1]  # new position k holds old option order[k]
    assert relabel("Option (A) is correct; options B and (d) are not. In (b), option a student", order) == \
        "Option (C) is correct; options D and (a) are not. In (b), option a student"


def test_c015_unlabelled_repeats_of_the_options_and_choose_the_correct_option_go():
    stem = ("Which way does the needle turn?\nChoose the correct option:\ntowards the south\ntowards the north\n"
            "vertically downwards\nvertically upwards")
    options = ["towards the north", "towards the south", "vertically upwards", "vertically downwards"]
    assert strip_inline_options(stem, options) == "Which way does the needle turn?"
    assert strip_inline_options("Choose the correct option:\nWhat is 2+2?") == "What is 2+2?"
