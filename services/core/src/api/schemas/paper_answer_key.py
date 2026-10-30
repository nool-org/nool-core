from pydantic import Field

from .common import CamelModel


class MarkingPointIO(CamelModel):
    point: str = Field(min_length=1, max_length=2000)
    marks: float = Field(gt=0)


class AnswerKeyItemOut(CamelModel):
    question_id: str
    answer: str
    marking_points: list[MarkingPointIO]
    total_marks: int


class AnswerKeyOut(CamelModel):
    items: list[AnswerKeyItemOut]
    # questions whose written key named a different option than the answer stored with the question (T-062):
    # left without a key - the teacher should check the question
    conflict_question_ids: list[str] = []


class UpdateAnswerKeyIn(CamelModel):
    answer: str | None = Field(default=None, min_length=1, max_length=6000)
    marking_points: list[MarkingPointIO] | None = Field(default=None, min_length=1, max_length=20)


class CreateParallelIn(CamelModel):
    name: str | None = Field(default=None, max_length=200)


class ParallelOut(CamelModel):
    paper_id: str
    name: str
