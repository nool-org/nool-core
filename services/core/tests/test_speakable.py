"""The one "can this be answered aloud" check (C-001, C-002), on the rows QA found."""

from src.services.speakable import problem, spoken_problem


def test_spoken_wordings_with_several_asks_or_equations_fail_and_single_asks_pass():
    bad = [
        "Define decomposition reaction. Give one example with heat, one with light and one with electricity. "
        "Also say why rotting vegetable waste into compost is an exothermic reaction.",
        "Tell me two main differences between roasting and calcination, with one example equation for each.",
        "Tell products and conditions when ethanol reacts with ethanoic acid. Tell products and conditions when "
        "an ester reacts with sodium hydroxide.",
        "Give reasons why zinc oxide is called amphoteric. Also, give reason why nitric acid gives no hydrogen.",
        "Why is changing ethanol to ethanoic acid called oxidation? Name one oxidising agent.",
        "Name one oxidising agent and say what it does to ethanol.",
    ]
    assert [r for r in map(spoken_problem, bad) if r is None] == []
    good = [
        "Why is turning vegetable waste into compost called an exothermic reaction?",
        "When we heat calcium carbonate, what does it break into, and why is this a decomposition reaction?",
        "What is the main difference between roasting and calcination of an ore?",
        "When ethanol reacts with ethanoic acid using concentrated sulphuric acid, what sweet-smelling "
        "substance is formed?",
        "Tell me why a piece of calcium starts floating when it is put in water.",
    ]
    assert [g for g in good if spoken_problem(g, ["What gas comes off?"])] == []
    assert spoken_problem(good[0], ["The gas is hydrogen."]) == "hint_tells"


def test_bank_questions_that_cant_be_spoken_fail():
    assert problem("Fill in the blanks: a. Acids change litmus to ________.") == "blanks"
    assert problem("State True or False: Respiration is an endothermic process.") == "options"
    assert problem(r"Is $2NH_3 + H_2SO_4 \rightarrow (NH_4)_2SO_4$ a decomposition reaction?") == "tex"
    assert problem("Identify the acid and base. Write chemical equation in support of your answer.") == "equation"
    assert problem("Give reasons for the following:\nZinc oxide is amphoteric.\nCalcium floats on water.") == \
        "multipart"
    assert problem("Why does a pencil look bent in a glass of water?") is None
