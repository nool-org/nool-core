import uuid

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .bloom import BloomLevel
from .mixins import IdMixin


class StudentTestResult(IdMixin, Base):
    __tablename__ = "student_test_results"
    __table_args__ = (UniqueConstraint("test_id", "student_id"),)

    test_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("voice_tests.id"))
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    mastery_percent: Mapped[int] = mapped_column(Integer)
    # Colearner's insight words + the fingerprint of the inputs they were
    # written from (services/insights.py). Deferred so code that never
    # touches it doesn't select or insert it.
    insights_words: Mapped[dict | None] = mapped_column(JSONB, nullable=True, deferred=True)


class StudentTestResultBloomScore(Base):
    __tablename__ = "student_test_result_bloom_scores"
    __table_args__ = (UniqueConstraint("student_test_result_id", "bloom_level"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    student_test_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("student_test_results.id")
    )
    bloom_level: Mapped[BloomLevel] = mapped_column(SAEnum(BloomLevel, name="bloom_level"))
    percent: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StudentTestResultTopicScore(Base):
    """Per-topic score of one result - the test's questions mapped to their
    topics (VoiceTestQuestion.topic_id). `answered` = questions of that topic
    the student actually answered."""

    __tablename__ = "student_test_result_topic_scores"
    __table_args__ = (UniqueConstraint("student_test_result_id", "topic_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    student_test_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("student_test_results.id")
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"))
    percent: Mapped[int] = mapped_column(Integer)
    answered: Mapped[int] = mapped_column(Integer)
