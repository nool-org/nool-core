import pytest
from pydantic import ValidationError

from src.api.schemas import GenerateAssessorContentIn, GenerateQuestionsIn, ReplacementCandidatesIn

CHAPTERS = [{"number": 1, "name": "Chemical Reactions"}]


def test_normal_requests_pass():
    GenerateQuestionsIn(chapters=CHAPTERS, total_questions=40, reference_questions=[{"text": "q"}] * 40)
    ReplacementCandidatesIn(chapters=CHAPTERS, bloom_level="APPLY", exclude_text="x" * 2000)
    GenerateAssessorContentIn(chapters=CHAPTERS, bloom_levels=["APPLY", "ANALYZE"])


@pytest.mark.parametrize("override", [
    {"subject": "x" * 201},
    {"topic_names": ["t"] * 301},
    {"chapters": CHAPTERS * 51},
    {"chapters": [{"number": 1, "name": "x" * 201}]},
    {"reference_questions": [{}] * 101},
    {"bloom_distribution": {f"L{i}": 1 for i in range(11)}},
    {"total_questions": 0},
    {"total_questions": 201},
])
def test_generate_questions_limits(override):
    with pytest.raises(ValidationError):
        GenerateQuestionsIn(**{"chapters": CHAPTERS, "total_questions": 10, **override})


def test_replacement_and_assessor_limits():
    with pytest.raises(ValidationError):
        ReplacementCandidatesIn(chapters=CHAPTERS, bloom_level="APPLY", exclude_text="x" * 5001)
    with pytest.raises(ValidationError):
        ReplacementCandidatesIn(chapters=CHAPTERS, bloom_level="APPLY", exclude_text="x", count=21)
    with pytest.raises(ValidationError):
        GenerateAssessorContentIn(chapters=CHAPTERS, num_questions=51)
