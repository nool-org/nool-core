from datetime import date, datetime

from pydantic import model_validator, Field, field_validator

from src.domain.models import SubscriptionStatus, UserStatus

from .bloom import BloomScore
from .common import CamelModel

# School Admin can only ever create/edit classes for grades 1 through 12 -
# see CreateClassIn/UpdateClassIn's validators below.
MIN_GRADE = 1
MAX_GRADE = 12


class SchoolTeacherOut(CamelModel):
    id: str
    display_name: str
    email: str
    phone_number: str | None
    employee_id: str | None
    class_ids: list[str]
    status: UserStatus
    must_change_password: bool


class InviteTeacherIn(CamelModel):
    email: str
    display_name: str
    phone_number: str | None = None
    employee_id: str | None = None


class InviteTeacherOut(CamelModel):
    id: str
    status: UserStatus
    temp_password: str


class UpdateTeacherStatusIn(CamelModel):
    status: UserStatus


class UpdateTeacherIn(CamelModel):
    """General profile edit, separate from the status-only endpoint above -
    partial update, only provided fields change."""

    display_name: str | None = None
    phone_number: str | None = None
    employee_id: str | None = None


class ResetPasswordOut(CamelModel):
    temp_password: str


class SendCredentialsEmailIn(CamelModel):
    """The admin's reviewed/edited draft (see CredentialReveal /
    SendCredentialsEmailModal on the frontend) - temp_password is passed
    through rather than re-derived, since only the plaintext-once value
    the caller already has in hand (never persisted server-side) can be
    included in the email body.
    """

    subject: str
    message: str
    temp_password: str


class SendCredentialsEmailOut(CamelModel):
    sent: bool


class UpgradeRequestIn(CamelModel):
    """Optional free-text note from the admin (e.g. "need 50 more student
    seats") - everything else (which school, current plan, current usage)
    is looked up server-side rather than trusted from the client.
    """

    message: str | None = None


class UpgradeRequestOut(CamelModel):
    recorded: bool
    emailed: bool


class SchoolStudentOut(CamelModel):
    id: str
    display_name: str
    email: str
    class_id: str
    roll_number: int
    guardian_name: str | None
    guardian_phone: str | None
    date_of_birth: date | None
    status: UserStatus
    must_change_password: bool


class CreateStudentIn(CamelModel):
    display_name: str
    email: str
    class_id: str
    roll_number: int
    guardian_name: str | None = None
    guardian_phone: str | None = None
    date_of_birth: date | None = None


class CreateStudentOut(CamelModel):
    id: str
    display_name: str
    email: str
    class_id: str
    roll_number: int
    status: UserStatus
    temp_password: str


class UpdateStudentIn(CamelModel):
    class_id: str | None = None
    status: UserStatus | None = None
    display_name: str | None = None
    guardian_name: str | None = None
    guardian_phone: str | None = None
    date_of_birth: date | None = None


class BulkRowResultOut(CamelModel):
    row: int
    status: str
    email: str | None = None
    temp_password: str | None = None
    error: str | None = None


class BulkImportResultOut(CamelModel):
    results: list[BulkRowResultOut]
    created_count: int
    error_count: int


class ClassAssignmentOut(CamelModel):
    teacher_id: str
    subject_id: str


class SchoolAdminClassOut(CamelModel):
    id: str
    grade: int
    section: str
    student_count: int
    assignments: list[ClassAssignmentOut]
    status: UserStatus


class CreateClassIn(CamelModel):
    grade: int
    section: str

    @field_validator("grade")
    @classmethod
    def _grade_in_range(cls, value: int) -> int:
        if not (MIN_GRADE <= value <= MAX_GRADE):
            raise ValueError(f"Grade must be between {MIN_GRADE} and {MAX_GRADE}.")
        return value


class UpdateClassIn(CamelModel):
    """Full edit of grade/section - both required (unlike the partial-update
    convention elsewhere in this file) since grade and section together are
    this class's identity (see the school_id+grade+section unique
    constraint); editing just one without the other isn't a meaningful
    partial update the way e.g. a teacher's phone number is.
    """

    grade: int
    section: str

    @field_validator("grade")
    @classmethod
    def _grade_in_range(cls, value: int) -> int:
        if not (MIN_GRADE <= value <= MAX_GRADE):
            raise ValueError(f"Grade must be between {MIN_GRADE} and {MAX_GRADE}.")
        return value


class UpdateClassStatusIn(CamelModel):
    status: UserStatus


class SchoolGradeOut(CamelModel):
    """The 'Class' level (e.g. "Class 10") - a school's Sections
    (SchoolAdminClassOut, above - unrenamed on the wire for backward
    compatibility with nool-apps) each belong to exactly one of these."""

    id: str
    grade: int
    section_count: int
    student_count: int
    status: UserStatus


class CreateGradeIn(CamelModel):
    grade: int

    @field_validator("grade")
    @classmethod
    def _grade_in_range(cls, value: int) -> int:
        if not (MIN_GRADE <= value <= MAX_GRADE):
            raise ValueError(f"Grade must be between {MIN_GRADE} and {MAX_GRADE}.")
        return value


class UpdateGradeStatusIn(CamelModel):
    status: UserStatus


class UpdateClassAssignmentsIn(CamelModel):
    assignments: list[ClassAssignmentOut]


class SubjectToggle(CamelModel):
    id: str
    name: str
    grade: int | None = None  # subjects are per grade; label "<name> · Grade <grade>"
    enabled: bool
    # Which grades this Subject applies to - see SubjectGrade's docstring.
    # An already-enabled subject can show up here with no grades (or a
    # grade that doesn't match this picker's own Class) if it was assigned
    # before that constraint existed; a *new* toggle is still rejected.
    grades: list[int] = []


class SchoolCurriculumOut(CamelModel):
    board: str
    subjects: list[SubjectToggle]


class GradeSubjectsOut(CamelModel):
    grade_id: str
    subjects: list[SubjectToggle]


class UpdateGradeSubjectsIn(CamelModel):
    subject_ids: list[str]


class UpdateSchoolCurriculumIn(CamelModel):
    subject_ids: list[str]


class CreateSchoolSubjectIn(CamelModel):
    """Lets a School Admin add a subject that isn't in the catalog yet -
    unlike admin_catalog's Super-Admin-only create, this both creates the
    global Subject row (or reuses one with a matching name, so two schools
    asking for "Sanskrit" don't end up with duplicate catalog rows) and
    immediately enables it for the caller's own school. See
    POST /school/curriculum/subjects.
    """

    name: str
    # Required, non-empty - same reasoning as admin_catalog's
    # CreateSubjectIn: without at least one grade, the enable-for-my-school
    # half of this call would have nothing eligible to grant. Unioned onto
    # an existing (possibly still ungraded) Subject rather than replacing
    # its links when reusing one by name. A subject is per grade, so this
    # must resolve to exactly one grade (`grade`, or a one-item `grades`).
    grade: int | None = None
    grades: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_grade(self) -> "CreateSchoolSubjectIn":
        from .admin_catalog import single_grade
        self.grade = single_grade(self.grade, self.grades)
        self.grades = [self.grade]
        return self


class SchoolDatasetOut(CamelModel):
    id: str
    name: str
    question_count: int
    description: str
    # Assigned to this school (catalog datasets) - always true for the
    # school's own datasets. Only a Super Admin can change it.
    enabled: bool
    # Kept for API compatibility - see Dataset.restricted's docstring.
    restricted: bool
    # True = the school's own question bank (Dataset.school_id), editable by
    # its School Admin; False = a platform catalog dataset.
    owned: bool = False
    subject_id: str | None = None
    subject_name: str | None = None
    grade: int | None = None
    # False when the dataset's subject is disabled for this school - the
    # dataset is then unusable (hidden from teachers/homework/papers) even
    # though it stays assigned/owned; re-enabling the subject restores it.
    subject_enabled: bool = True


class UpdateSchoolDatasetsIn(CamelModel):
    dataset_ids: list[str]


class ClassBreakdownOut(CamelModel):
    class_id: str
    label: str
    mastery_avg_percent: int
    # the class's latest challenge average minus its first one's (0 until it has two)
    improvement_percent: int
    challenges_taken: int = 0
    students_assessed: int = 0


class WeakChapterOut(CamelModel):
    """A chapter the school is weakest on, across every class that took a challenge on it."""
    chapter_id: str
    chapter: str
    subject: str
    average_percent: int
    students: int
    classes: list[str]


class SlippingStudentOut(CamelModel):
    """A student whose latest challenge fell well below their own earlier average, or is very low."""
    student_id: str
    name: str
    class_label: str
    chapter: str
    latest_percent: int
    earlier_avg_percent: int | None


class MasteryTrendPointOut(CamelModel):
    """One week's average mastery across every Test taken at this school -
    computed at read time from StudentTestResult/VoiceTest timestamps,
    not a stored snapshot, so it's always exactly consistent with the
    rest of analytics."""

    period_label: str
    mastery_avg_percent: int
    test_count: int


class SchoolAnalyticsOut(CamelModel):
    school_mastery_avg_percent: int
    class_breakdown: list[ClassBreakdownOut]
    bloom_averages: list[BloomScore]
    mastery_trend: list[MasteryTrendPointOut]
    weak_chapters: list[WeakChapterOut] = []
    slipping_students: list[SlippingStudentOut] = []


class SchoolSubscriptionOut(CamelModel):
    """Read-only for School Admin - same record Super Admin manages at
    /api/v1/admin/schools/{schoolId}/subscription, scoped to the caller's
    own school. Usage fields make plan limits visible proactively, instead
    of only ever surfacing as a 409 at the moment of creation - a null
    *_limit means that resource is unlimited on this plan (see Plan's own
    test_limit/question_paper_limit doc comment).
    """

    school_id: str
    plan_id: str
    plan_name: str
    price_label: str
    discount_percent: int
    status: SubscriptionStatus
    renews_at: datetime

    teacher_count: int
    teacher_limit: int
    student_count: int
    student_limit: int
    test_count: int
    test_limit: int | None
    question_paper_count: int
    question_paper_limit: int | None


class SchoolAdminMeOut(CamelModel):
    """The signed-in School Admin's own account - separate from the
    claim-only /me (see profile.py), which every role shares and doesn't
    carry phoneNumber. Self-service only: there is no endpoint for a
    School Admin to view/edit another School Admin.
    """

    id: str
    display_name: str
    email: str
    phone_number: str | None
    school_name: str


class UpdateSchoolAdminMeIn(CamelModel):
    """Partial update, only provided fields change. No email field - email
    changes would desync from the Firebase account and aren't supported
    anywhere else in this API either.
    """

    display_name: str | None = None
    phone_number: str | None = None
