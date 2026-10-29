"""V-020: create requests refuse negative, zero and absurd numbers and a huge name."""
import pytest
from pydantic import ValidationError

from src.api.schemas.question_paper import CreateQuestionPaperIn
from src.api.schemas.voice_test import CreateTestIn

PAPER = dict(name="Unit test", examType="UNIT", board="CBSE", grade=10, language="English", subjectId="s")
TEST = dict(classId="c", subjectId="s", chapterId="ch", bloomLevels=["REMEMBER"], durationMinutes=10,
            completionWindowHours=24, target={"mode": "WHOLE_CLASS"})


@pytest.mark.parametrize("bad", [
    {"totalMarks": -5}, {"totalMarks": 10**15}, {"durationMinutes": 0}, {"name": "x" * 10_000},
    {"sections": [{"label": "A", "questionCount": -1, "marksEach": 10**9}]},
])
def test_paper_rejects(bad):
    with pytest.raises(ValidationError):
        CreateQuestionPaperIn.model_validate({**PAPER, **bad})


@pytest.mark.parametrize("bad", [{"durationMinutes": -5}, {"completionWindowHours": 0}, {"bloomLevels": []}])
def test_test_rejects(bad):
    with pytest.raises(ValidationError):
        CreateTestIn.model_validate({**TEST, **bad})


def test_sane_values_pass():
    CreateQuestionPaperIn.model_validate({**PAPER, "totalMarks": 80, "durationMinutes": 180,
                                          "sections": [{"label": "A", "questionCount": 5, "marksEach": 2}]})
    CreateTestIn.model_validate(TEST)
