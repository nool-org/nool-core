from types import SimpleNamespace

from src.api.routes.question_paper import is_choice_question
from src.domain.models.custom_question import QuestionType


def test_mcq_gets_no_text_only_ai_candidates():  # C-017
    assert is_choice_question(SimpleNamespace(options=["a", "b", "c", "d"], question_type=None))
    assert is_choice_question(SimpleNamespace(options=None, question_type=QuestionType.MCQ))
    assert not is_choice_question(SimpleNamespace(options=None, question_type=QuestionType.SHORT_ANSWER))
