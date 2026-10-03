from pydantic import BaseModel


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
    name: str


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
    subject: str = "Science"
    board: str = "CBSE"
    grade: int = 10
    chapters: list[ChapterRef]
    topic_names: list[str] = []
    bloom_distribution: dict[str, int] = {}
    difficulty_distribution: dict[str, int] = {}
    question_types: list[str] = []
    total_questions: int
    # Sample of the paper's selected question-bank (QA dataset) questions -
    # see generator.py's REFERENCE_GUIDANCE for how they're used.
    reference_questions: list[dict] = []


class ReplacementCandidatesIn(BaseModel):
    subject: str = "Science"
    board: str = "CBSE"
    grade: int = 10
    chapters: list[ChapterRef]
    topic_names: list[str] = []
    bloom_level: str
    difficulty: str = "MEDIUM"
    exclude_text: str
    count: int = 3


# ---- Voice Test (AI Assessor) generation -----------------------------------

class GenerateAssessorContentIn(BaseModel):
    subject: str = "Science"
    board: str = "CBSE"
    grade: int = 10
    chapters: list[ChapterRef]
    topic_names: list[str] = []
    bloom_levels: list[str] = []
    num_questions: int = 4


class GenerateAssessorContentOut(BaseModel):
    reference_questions: list[str]
    textbook_context: str
