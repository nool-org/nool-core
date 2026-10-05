from typing import Annotated

from pydantic import BaseModel, Field

# Request size limits - generation requests end up verbatim in a paid model
# prompt, so cap free text and list sizes well above any real use.
ShortText = Annotated[str, Field(max_length=200)]
LongText = Annotated[str, Field(max_length=5000)]
Count = Annotated[int, Field(ge=1, le=200)]


# ---- Curriculum tree ------------------------------------------------------

class SubtopicTreeOut(BaseModel):
    ref: str
    number: str
    name: str


class TopicTreeOut(BaseModel):
    ref: str
    number: str
    name: str
    subtopics: list[SubtopicTreeOut]


class ChapterTreeOut(BaseModel):
    ref: str
    number: int
    name: str
    topics: list[TopicTreeOut]


class CurriculumTreeOut(BaseModel):
    board: str
    grade: int
    subject: str
    chapters: list[ChapterTreeOut]


class CurriculumSubjectOut(BaseModel):
    name: str
    grade: int
    chapter_count: int


# ---- Question generation ---------------------------------------------------

class ChapterRef(BaseModel):
    number: int
    name: ShortText


class GeneratedQuestionOut(BaseModel):
    text: str
    answer: str
    bloom_level: str
    difficulty: str
    question_type: str
    chapter_number: int
    topic: str
    source_refs: list[str]


class GenerateQuestionsOut(BaseModel):
    questions: list[GeneratedQuestionOut]


class GenerateQuestionsIn(BaseModel):
    subject: ShortText = "Science"
    board: ShortText = "CBSE"
    grade: int = 10
    chapters: list[ChapterRef] = Field(max_length=50)
    topic_names: list[ShortText] = Field(default=[], max_length=300)
    bloom_distribution: dict[ShortText, int] = Field(default={}, max_length=10)
    difficulty_distribution: dict[ShortText, int] = Field(default={}, max_length=10)
    question_types: list[ShortText] = Field(default=[], max_length=10)
    total_questions: Count
    # Sample of the paper's selected question-bank (QA dataset) questions -
    # see generator.py's REFERENCE_GUIDANCE for how they're used.
    # Core sends at most 40 (its _REFERENCE_POOL_SIZE).
    reference_questions: list[dict] = Field(default=[], max_length=100)


class ReplacementCandidatesIn(BaseModel):
    subject: ShortText = "Science"
    board: ShortText = "CBSE"
    grade: int = 10
    chapters: list[ChapterRef] = Field(max_length=50)
    topic_names: list[ShortText] = Field(default=[], max_length=300)
    bloom_level: ShortText
    difficulty: ShortText = "MEDIUM"
    exclude_text: LongText
    count: Annotated[int, Field(ge=1, le=20)] = 3


# ---- Voice Test (AI Assessor) generation -----------------------------------

class GenerateAssessorContentIn(BaseModel):
    subject: ShortText = "Science"
    board: ShortText = "CBSE"
    grade: int = 10
    chapters: list[ChapterRef] = Field(max_length=50)
    topic_names: list[ShortText] = Field(default=[], max_length=300)
    bloom_levels: list[ShortText] = Field(default=[], max_length=10)
    num_questions: Annotated[int, Field(ge=1, le=50)] = 4


class GenerateAssessorContentOut(BaseModel):
    reference_questions: list[str]
    textbook_context: str
