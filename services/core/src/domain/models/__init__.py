"""Every ORM model in Core, imported here so Base.metadata sees the full
schema - database/migrations/env.py imports this module for exactly that
side effect before pointing Alembic's target_metadata at Base.metadata.
"""

from ..base import Base
from .ai_assessor import AiAssessorSession, AiAssessorSessionBloomLevel, AiAssessorSessionRecord
from .assistant import AssistantMessage, ChatRole
from .bloom import BloomLevel
from .curriculum import Chapter, GradeSubject, SchoolCurriculum, Subject, SubjectGrade, Subtopic, Topic
from .custom_question import CustomQuestion, QuestionType
from .dataset import Dataset, DatasetQuestion, DatasetType, QuestionEmbedding, SchoolDataset
from .gamification import StudentDailyGoalCompletion, StudentGamification
from .journey import StudentChapterProgress
from .foundation import (
    AuditLog,
    Feature,
    Plan,
    Role,
    School,
    SchoolStatus,
    Subscription,
    SubscriptionStatus,
    UpgradeRequest,
    UpgradeRequestStatus,
    User,
    UserStatus,
)
from .homework import (
    Homework,
    HomeworkBloomDistribution,
    HomeworkDataset,
    HomeworkDifficulty,
    HomeworkQuestion,
    HomeworkStatus,
    HomeworkTargetStudent,
    StudentHomeworkProgress,
    StudentHomeworkStatus,
)
from .import_job import ImportJob, ImportJobType
from .practice_bank import PracticeBankEntry
from .reporting import ReportConfiguration, ReportShare
from .leaderboard import StudentPoints
from .question_paper import (
    PaperDifficultyLevel,
    QuestionPaper,
    QuestionPaperBloomDistribution,
    QuestionPaperChapter,
    QuestionPaperDatasetShare,
    QuestionPaperDifficultyDistribution,
    QuestionPaperQuestion,
    QuestionPaperSection,
    QuestionPaperStatus,
    QuestionPaperTopic,
    QuestionPaperValidation,
)
from .roster import SchoolClass, SchoolGrade, StudentProfile, TeacherClassAssignment
from .support_ticket import SupportTicket, SupportTicketComment, TicketStatus
from .test_result import StudentTestResult, StudentTestResultBloomScore, StudentTestResultTopicScore
from .voice_test import (
    AssignmentTargetMode,
    TestStatus,
    VoiceTest,
    VoiceTestBloomLevel,
    VoiceTestQuestion,
    VoiceTestQuestionStatus,
    VoiceTestTargetStudent,
)

__all__ = [
    "Base",
    "AiAssessorSession",
    "AiAssessorSessionBloomLevel",
    "AiAssessorSessionRecord",
    "AssistantMessage",
    "ChatRole",
    "BloomLevel",
    "Chapter",
    "SchoolCurriculum",
    "GradeSubject",
    "Subject",
    "SubjectGrade",
    "Subtopic",
    "Topic",
    "CustomQuestion",
    "QuestionType",
    "Dataset",
    "DatasetQuestion",
    "DatasetType",
    "QuestionEmbedding",
    "SchoolDataset",
    "StudentDailyGoalCompletion",
    "StudentGamification",
    "StudentChapterProgress",
    "AuditLog",
    "Feature",
    "Plan",
    "Role",
    "School",
    "SchoolStatus",
    "Subscription",
    "UpgradeRequest",
    "UpgradeRequestStatus",
    "SubscriptionStatus",
    "User",
    "UserStatus",
    "Homework",
    "HomeworkBloomDistribution",
    "HomeworkDataset",
    "HomeworkDifficulty",
    "HomeworkQuestion",
    "HomeworkStatus",
    "HomeworkTargetStudent",
    "StudentHomeworkProgress",
    "StudentHomeworkStatus",
    "ImportJob",
    "ImportJobType",
    "PracticeBankEntry",
    "ReportConfiguration",
    "ReportShare",
    "StudentPoints",
    "PaperDifficultyLevel",
    "QuestionPaper",
    "QuestionPaperBloomDistribution",
    "QuestionPaperChapter",
    "QuestionPaperDatasetShare",
    "QuestionPaperDifficultyDistribution",
    "QuestionPaperQuestion",
    "QuestionPaperSection",
    "QuestionPaperStatus",
    "QuestionPaperTopic",
    "QuestionPaperValidation",
    "SchoolClass",
    "SchoolGrade",
    "StudentProfile",
    "TeacherClassAssignment",
    "SupportTicket",
    "SupportTicketComment",
    "TicketStatus",
    "StudentTestResult",
    "StudentTestResultBloomScore",
    "StudentTestResultTopicScore",
    "AssignmentTargetMode",
    "TestStatus",
    "VoiceTest",
    "VoiceTestBloomLevel",
    "VoiceTestQuestion",
    "VoiceTestQuestionStatus",
    "VoiceTestTargetStudent",
]
