import uuid

from src.domain.models import BloomLevel
from src.services.evaluation_service import evaluate_colearner_session, topic_scores


def test_returns_none_for_empty_report():
    assert evaluate_colearner_session(None, [BloomLevel.UNDERSTAND]) is None
    assert evaluate_colearner_session({}, [BloomLevel.UNDERSTAND]) is None
    assert evaluate_colearner_session({"qa_transcripts": []}, [BloomLevel.UNDERSTAND]) is None


def test_returns_none_when_no_entry_has_a_score():
    report = {"qa_transcripts": [{"blooms_attribute": "Understanding", "question_asked": "x"}]}
    assert evaluate_colearner_session(report, [BloomLevel.UNDERSTAND]) is None


def test_averages_scores_per_bloom_level_and_overall():
    report = {
        "qa_transcripts": [
            {"blooms_attribute": "Understanding", "score_percent": 80},
            {"blooms_attribute": "Applying", "score_percent": 40},
            {"blooms_attribute": "Applying", "score_percent": 60},
        ]
    }
    result = evaluate_colearner_session(report, [BloomLevel.UNDERSTAND, BloomLevel.APPLY])

    assert result is not None
    mastery_percent, bloom_scores = result
    assert mastery_percent == 60  # (80 + 40 + 60) / 3
    assert bloom_scores == {BloomLevel.UNDERSTAND: 80, BloomLevel.APPLY: 50}


def test_bloom_level_with_no_scored_entries_falls_back_to_overall_mastery():
    report = {"qa_transcripts": [{"blooms_attribute": "Understanding", "score_percent": 90}]}
    result = evaluate_colearner_session(report, [BloomLevel.UNDERSTAND, BloomLevel.ANALYZE])

    assert result is not None
    mastery_percent, bloom_scores = result
    assert mastery_percent == 90
    assert bloom_scores == {BloomLevel.UNDERSTAND: 90, BloomLevel.ANALYZE: 90}


def test_clamps_out_of_range_scores():
    report = {"qa_transcripts": [{"blooms_attribute": "Understanding", "score_percent": 140}]}
    result = evaluate_colearner_session(report, [BloomLevel.UNDERSTAND])

    assert result is not None
    mastery_percent, _ = result
    assert mastery_percent == 100


def test_topic_scores_map_question_numbers_to_topics():
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    report = {"qa_transcripts": [
        {"question_number": 1, "score_percent": 80, "student_response": "x"},
        {"question_number": 2, "score_percent": 41, "student_response": " "},
        {"question_number": 3, "score_percent": 150, "student_response": "y"},
        {"question_number": 4, "score_percent": 10, "student_response": "z"},  # topic-less question
        {"question_number": 5, "student_response": "unscored"},
    ]}
    assert topic_scores(report, {1: t1, 2: t1, 3: t2, 4: None, 5: t2}) == {t1: (60, 1), t2: (100, 1)}
    assert topic_scores(None, {1: t1}) == {}
