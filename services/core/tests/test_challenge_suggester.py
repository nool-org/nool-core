"""The teacher's challenge suggestion (services/challenge_suggester.py): pure rules."""
from src.services import challenge_suggester as cs


def _topic(name, avg, quizzed=3, per_bloom=None, stuck=0):
    return {"topic_id": name, "topic_name": name, "quizzed": quizzed, "average_percent": avg,
            "per_bloom": per_bloom or {}, "needs_teacher": [{}] * stuck}


def _work(name, equations=0.0, calculations=0.0):
    return {"topic_id": name, "name": name, "equations": equations, "calculations": calculations}


def test_maths_suggests_fix_milo_focused_on_the_weak_topics():
    out = cs.decide("Mathematics", "Arithmetic Progressions", [
        _topic("5.2 ARITHMETIC PROGRESSIONS", 82, per_bloom={"APPLY": 80}),
        _topic("5.3 NTH TERM OF AN AP", 45, per_bloom={"APPLY": 40, "ANALYZE": 55}, stuck=2),
        _topic("5.4 SUM OF FIRST N TERMS", 60, per_bloom={"REMEMBER": 90}),
    ], taken=set(), work=[])
    assert [o["format"] for o in out["options"]] == ["fix_steps"]  # no voice in Maths
    # weakest first, only those under 70%
    assert [t["topic_id"] for t in out["focus_topics"]] == ["5.3 NTH TERM OF AN AP", "5.4 SUM OF FIRST N TERMS"]
    assert out["bloom_levels"] == ["UNDERSTAND", "APPLY", "ANALYZE"]
    # skills averaged over topics, weighted by takers: Apply 60%, Analyze 55%, Remember 90%
    assert any("weakest at Analyze (55%)" in e for e in out["evidence"])
    assert any("2 students still stuck" in e for e in out["evidence"])
    assert out["note"] is None and out["taken"] is False


def test_an_explained_chapter_suggests_speak_with_milo_first_and_its_weakest_skill():
    out = cs.decide("Science", "Our Environment", [_topic("13.1", 70, per_bloom={"ANALYZE": 30, "REMEMBER": 90})],
                    taken=set(), work=[_work("13.1", equations=0.09), _work("13.2", calculations=0.01)])
    assert [o["format"] for o in out["options"]] == ["voice", "fix_steps"]
    assert out["options"][0]["fix_cards"] == 0
    assert "mostly facts and terms" in out["options"][0]["why"]
    assert out["bloom_levels"] == ["REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE"]  # facts + its weakest skill


def test_a_mixed_chapter_suggests_speak_with_milo_with_fix_milo_cards_on_its_equations():
    # balancing and reactions are worked on paper, oxidation is explained
    out = cs.decide("Science", "Chemical Reactions and Equations", [_topic("1.2", 40), _topic("1.1", 80)],
                    taken=set(), work=[_work("1.1", equations=0.43, calculations=0.28),
                                       _work("1.2", equations=0.36), _work("1.3", equations=0.04)])
    best, other = out["options"]
    assert (best["format"], best["fix_cards"], other["format"]) == ("voice", 1, "fix_steps")
    assert best["label"] == "Speak with Milo + Fix Milo cards"
    assert [t["topic_id"] for t in other["topics"]] == ["1.2", "1.1"]  # the class's weakest first
    assert "an equation" in best["why"]
    assert "Worked on paper: 1.2, 1.1. Explained in words: 1.3." in out["evidence"]


def test_a_mostly_worked_chapter_leads_with_fix_milo():
    work = [_work(f"11.{i}", calculations=0.2) for i in range(1, 6)] + [_work("11.3", calculations=0.07)]
    out = cs.decide("Science", "Electricity", [], taken=set(), work=work)
    assert [o["format"] for o in out["options"]] == ["fix_steps", "voice"]
    assert "Worked on paper (calculations)" in out["options"][0]["why"]


def test_a_format_already_set_on_the_chapter_is_left_out():
    work = [_work("1.1", equations=0.5), _work("1.3")]
    out = cs.decide("Science", "Chemical Reactions and Equations", [], taken={"voice"}, work=work)
    assert [o["format"] for o in out["options"]] == ["fix_steps"] and out["taken"] is False
    assert any("Already set on this chapter: Speak with Milo." in e for e in out["evidence"])
    assert cs.decide("Science", "Chemical Reactions and Equations", [], taken={"voice", "fix_steps"},
                     work=work)["taken"] is True


def test_without_results_it_says_the_suggestion_is_from_the_chapter_alone():
    assert "marked taught yet" in cs.decide("Science", "Electricity", [], taken=set(), work=[])["note"]
    assert "No checkpoint results" in cs.decide("Science", "Electricity", [_topic("11.1", None, quizzed=0)],
                                                 taken=set(), work=[])["note"]
