from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Input size caps - every field below is client-supplied and ends up in an
# LLM prompt or a decode buffer, so nothing is unbounded.
MAX_AUDIO_BASE64_CHARS = 7_000_000  # ~5 MB of audio
MAX_USER_TEXT_CHARS = 4_000
MAX_TEXTBOOK_CONTEXT_CHARS = 20_000
ShortText = Annotated[str, StringConstraints(max_length=200)]
ReferenceQuestion = Annotated[str, StringConstraints(max_length=500)]
RubricText = Annotated[str, StringConstraints(max_length=500)]


class LiveQuestion(BaseModel):
    """One prepared voice-test question (contract C3) - the realtime co-learner asks exactly these."""
    id: Union[int, Annotated[str, StringConstraints(max_length=64)]]
    order: int = Field(..., ge=1, le=10)
    bloom_level: ShortText
    spoken_text: str = Field(..., min_length=1, max_length=1_000)
    expected_points: List[RubricText] = Field(default_factory=list, max_length=6)
    misconceptions: List[RubricText] = Field(default_factory=list, max_length=6)
    hints: List[RubricText] = Field(default_factory=list, max_length=4)


class SessionConfig(BaseModel):
    """Pedagogical session configuration only. Model-specific data (LLM
    model, STT engine, TTS voice) is deliberately NOT part of this schema -
    each pipeline fixes its own models as constants in its own service
    module (realtime_service.py's REALTIME_MODEL/TRANSCRIBE_MODEL,
    cascaded_service.py's STT/LLM/TTS_MODEL), so a caller cannot pick a
    model.
    """

    grade: ShortText = Field(default="10th Grade", description="Grade / level of the student")
    subject: ShortText = Field(default="Science (Physics)", description="Academic subject")
    chapter_name: ShortText = Field(default="Electricity & Circuits", description="Name of the chapter / topic")
    num_questions: int = Field(default=2, ge=1, le=10, description="Target number of questions to evaluate")
    blooms_attributes: List[ShortText] = Field(
        default_factory=lambda: ["Understanding", "Applying", "Analyzing"],
        max_length=10,
        description="Target Bloom's Taxonomy cognitive levels to assess (e.g. Understanding, Applying, Analyzing)"
    )
    textbook_context: Optional[str] = Field(
        default=None,
        max_length=MAX_TEXTBOOK_CONTEXT_CHARS,
        description="Comprehensive textbook concepts, principles, formulas, and syllabus context passed in JSON"
    )
    reference_questions: List[ReferenceQuestion] = Field(
        default_factory=list,
        max_length=20,
        description="Reference topic inspirations and core concepts for the co-learner to formulate creative scenario questions"
    )
    session_max_time_seconds: int = Field(
        default=300,
        description="Maximum session time limit in seconds (default 300s / 5 minutes)"
    )
    questions: Optional[List[LiveQuestion]] = Field(
        default=None,
        max_length=10,
        description="Prepared questions (realtime only). When given, the co-learner asks exactly these, in order, "
                    "and num_questions follows their count."
    )

    @model_validator(mode="after")
    def _questions_set_count(self):
        if self.questions:
            self.questions.sort(key=lambda q: q.order)
            self.num_questions = len(self.questions)
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "grade": "10th Grade",
                "subject": "Science (Physics)",
                "chapter_name": "Electricity & Circuits",
                "num_questions": 2,
                "textbook_context": "Chapter 11: Electric current is the rate of flow of charge (I = Q/t). Ohm's law states that V = IR at constant temperature. Resistance factors: R = rho * l / A. Joule's heating law: H = I^2 * R * t. Heating elements use high resistance nichrome which glows red hot, while cords use low resistance copper which stays cool.",
                "reference_questions": [
                    "Why does the cord of an electric heater not glow while the heating element glows red hot?",
                    "If a metal wire of length L and resistance R is stretched to double its length, how do its resistance and resistivity change?"
                ]
            }
        }
    )


class CoLearnerRequest(BaseModel):
    session_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Session ID. Omit or pass null on Turn 0 to initialize session and receive greeting; pass returned ID on subsequent turns."
    )
    config: Optional[SessionConfig] = Field(
        default=None,
        description="Session configuration. Required when initiating a new session (Turn 0)."
    )
    user_audio_base64: Optional[str] = Field(
        default=None,
        max_length=MAX_AUDIO_BASE64_CHARS,
        description="Base64-encoded audio bytes (WAV/WebM/MP3) recorded from student microphone."
    )
    user_text: Optional[str] = Field(
        default=None,
        max_length=MAX_USER_TEXT_CHARS,
        description="Direct text response (convenient for testing conversational turns in Swagger UI without recording audio)."
    )
    time_limit_reached: bool = Field(
        default=False,
        description="Flag indicating session maximum time limit reached on client or server timer."
    )
    is_hint_request: bool = Field(
        default=False,
        description="True when the student is asking for a guiding nudge on the CURRENT question, not submitting an answer - never advances the turn, never reveals or confirms correctness, and is excluded from the graded transcript."
    )


class CoLearnerResponse(BaseModel):
    session_id: str = Field(..., description="Unique UUID for this assessment session")
    turn_index: int = Field(..., description="Current turn number (0 for greeting, 1+ for questions)")
    student_transcript: Optional[str] = Field(
        default=None,
        description="Transcribed or submitted student response (from STT or text fallback)"
    )
    co_learner_text: str = Field(
        ...,
        description="Spoken text generated by the Co-Learner Companion"
    )
    co_learner_audio_base64: Optional[str] = Field(
        default=None,
        description="Base64-encoded audio stream (audio/mpeg) for direct browser playback"
    )
    audio_mime_type: str = Field(default="audio/mpeg", description="MIME type of the audio stream")
    is_completed: bool = Field(
        default=False,
        description="True when all questions are completed and farewell has been delivered"
    )
    report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured Bloom's Taxonomy Diagnostic Report generated upon session completion"
    )
    turn_metrics: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Per-turn resource and cost metrics (input_tokens, output_tokens, audio_seconds, turn_cost_usd)"
    )
    cost_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Comprehensive cumulative session Cost Analysis Report generated upon session completion"
    )
    is_hint_reply: bool = Field(
        default=False,
        description="True when co_learner_text is a guiding hint, not a reply to a submitted answer or a new question - the caller should keep the student on the same question."
    )


class HomeworkInsightRequest(BaseModel):
    """Stateless, one-shot request - see
    src/services/homework_insight_service.py. No session_id: unlike the
    live AI Assessor conversation, this call carries everything it needs in
    the request itself. Still requires an authenticated caller (see
    src/api/routes/homework_insight.py).
    """
    topic: ShortText = Field(..., description="The Homework's gap_topic (usually a chapter name).")
    grade: ShortText = Field(..., description="e.g. 'Grade 10' - keeps the explanation pitched right.")
    subject: ShortText = Field(..., description="e.g. 'Science'.")
    mastery_percent: int = Field(..., description="The student's overall score on the originating Test.")
    weak_bloom_level: ShortText = Field(..., description="The single Bloom's level the student scored lowest on.")
    textbook_context: Optional[str] = Field(
        default=None,
        max_length=MAX_TEXTBOOK_CONTEXT_CHARS,
        description="The Test's syllabus excerpt, if any - grounds the paragraph/section reference in real material instead of inventing one."
    )


class HomeworkInsightResponse(BaseModel):
    what_needs_understanding: str
    references: List[Dict[str, str]] = Field(
        description="[{title, subtitle}, ...] - a specific concept/section to revisit, not a generic 'class notes' pointer."
    )
    key_idea_title: str
    key_idea_body: str
    connection_prompt: str = Field(
        description="A real-world analogy connecting the weak concept to everyday experience."
    )
