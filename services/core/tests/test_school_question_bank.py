"""A school's own question banks (api/routes/school_question_bank.py) and
assignment-only access to catalog datasets."""

import io
import uuid

import pytest
from starlette.datastructures import UploadFile

from shared.errors import NotFoundError, ValidationError
from src.api.routes import admin as admin_routes
from src.api.routes import school_admin as school_admin_routes
from src.api.routes import school_question_bank as bank
from src.api.schemas.admin_catalog import CreateDatasetQuestionIn
from src.api.schemas.school_admin import UpdateSchoolDatasetsIn
from src.domain.models import (
    BloomLevel, Chapter, Dataset, QuestionType, Role, School, SchoolClass, SchoolCurriculum, Subject,
    TeacherClassAssignment, Topic,
    User, UserStatus,
)
from src.domain.models.curriculum import Subtopic
from src.domain.models.question_paper import PaperDifficultyLevel
from src.repositories.dataset_access import visible_datasets


def _user(role, school):
    return User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name=role.value,
                role=role, school_id=school.id if school else None, status=UserStatus.ACTIVE)


async def _seed(db_session) -> dict:
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    schools = [School(name=f"S{i}-{uuid.uuid4()}", board="CBSE", city="Pune", contact_email=f"{uuid.uuid4()}@b.com")
               for i in range(2)]
    db_session.add_all([subject, *schools])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="11 Electricity", order_index=11, grade=10)
    sections = [SchoolClass(school_id=s.id, grade=10, section="A") for s in schools]
    admin, other_admin = _user(Role.SCHOOL_ADMIN, schools[0]), _user(Role.SCHOOL_ADMIN, schools[1])
    teacher, other_teacher = _user(Role.TEACHER, schools[0]), _user(Role.TEACHER, schools[1])
    super_admin = _user(Role.SUPER_ADMIN, None)
    db_session.add_all([chapter, *sections, admin, other_admin, teacher, other_teacher, super_admin])
    await db_session.flush()
    topic = Topic(chapter_id=chapter.id, name="11.6 RESISTANCE OF A SYSTEM OF RESISTORS")
    catalog = Dataset(name=f"Catalog-{uuid.uuid4()}", question_count=0, description="", subject_id=subject.id, grade=10)
    db_session.add_all([topic, catalog, *(TeacherClassAssignment(teacher_id=t.id, class_id=c.id, subject_id=subject.id)
                                          for t, c in ((teacher, sections[0]), (other_teacher, sections[1])))])
    await db_session.flush()
    subtopic = Subtopic(topic_id=topic.id, name="11.6.1 Resistors in Series")
    # a Super Admin gives both schools the subject
    db_session.add_all([subtopic, *(SchoolCurriculum(school_id=sc.id, subject_id=subject.id, enabled=True) for sc in schools)])
    await db_session.flush()
    return {"subject": subject, "schools": schools, "chapter": chapter, "topic": topic, "subtopic": subtopic,
            "catalog": catalog, "admin": admin, "other_admin": other_admin, "teacher": teacher,
            "other_teacher": other_teacher, "super_admin": super_admin}


async def _new_bank(db_session, s, name="Unit tests"):
    return await bank.create_school_dataset(
        bank.CreateSchoolDatasetIn(name=name, subject_id=str(s["subject"].id), grade=10),
        user=s["admin"], session=db_session)


@pytest.mark.asyncio
async def test_a_school_bank_takes_questions_with_all_fields_and_stays_inside_the_school(db_session):
    s = await _seed(db_session)
    own = await _new_bank(db_session, s)
    q = await bank.create_school_dataset_question(
        own.id, CreateDatasetQuestionIn(
            subtopic_id=str(s["subtopic"].id), bloom_level=BloomLevel.APPLY, question_type=QuestionType.SHORT_ANSWER,
            difficulty=PaperDifficultyLevel.HARD, marks=3, text="Two 4 ohm resistors in series?", answer="8 ohm",
            solution="R = 4 + 4"),
        user=s["admin"], session=db_session)
    # a subtopic alone places the question in its topic and chapter
    assert (q.chapter_id, q.topic_id, q.subtopic_id) == (str(s["chapter"].id), str(s["topic"].id), str(s["subtopic"].id))
    assert (q.difficulty, q.marks, q.solution) == (PaperDifficultyLevel.HARD, 3, "R = 4 + 4")

    mine = {str(d.id) for d in await visible_datasets(db_session, s["teacher"])}
    theirs = {str(d.id) for d in await visible_datasets(db_session, s["other_teacher"])}
    assert own.id in mine and own.id not in theirs
    with pytest.raises(NotFoundError):  # another school can't touch it
        await bank.update_school_dataset(own.id, bank.UpdateSchoolDatasetIn(name="x"),
                                         user=s["other_admin"], session=db_session)


@pytest.mark.asyncio
async def test_catalog_datasets_are_assigned_only_by_super_admin_and_never_a_school_bank(db_session):
    s = await _seed(db_session)
    own = await _new_bank(db_session, s)
    listing = await school_admin_routes.get_school_datasets(user=s["admin"], session=db_session)
    assert {d.id for d in listing.items} == {own.id}                      # unassigned catalog hidden

    await admin_routes.update_datasets_as_admin(
        str(s["schools"][0].id), UpdateSchoolDatasetsIn(dataset_ids=[str(s["catalog"].id)]),
        actor=s["super_admin"], session=db_session)
    listing = await school_admin_routes.get_school_datasets(user=s["admin"], session=db_session)
    assert {(d.id, d.owned) for d in listing.items} == {(own.id, True), (str(s["catalog"].id), False)}

    with pytest.raises(ValidationError):                                  # school bank -> other school: refused
        await admin_routes.update_datasets_as_admin(
            str(s["schools"][1].id), UpdateSchoolDatasetsIn(dataset_ids=[own.id]),
            actor=s["super_admin"], session=db_session)
    with pytest.raises(NotFoundError):                                    # school can't edit catalog questions
        await bank.create_school_dataset_question(
            str(s["catalog"].id), CreateDatasetQuestionIn(
                chapter_id=str(s["chapter"].id), bloom_level=BloomLevel.REMEMBER,
                question_type=QuestionType.SHORT_ANSWER, text="t", answer="a"),
            user=s["admin"], session=db_session)
    admin_view = await admin_routes.get_datasets_as_admin(str(s["schools"][1].id), _=s["super_admin"], session=db_session)
    assert own.id not in {d.id for d in admin_view.items}                 # other school's bank not even listed


@pytest.mark.asyncio
async def test_bulk_import_matches_by_name_or_number_and_reports_bad_rows(db_session):
    s = await _seed(db_session)
    own = await _new_bank(db_session, s)
    csv = ("chapter,topic,subtopic,bloomLevel,questionType,difficulty,marks,text,options,answer,solution\n"
           "11,11.6,11.6.1,APPLY,SHORT_ANSWER,MEDIUM,2,Series of 2 and 3 ohm?,,5 ohm,2+3\n"
           "11 Electricity,,,REMEMBER,MCQ,EASY,1,Unit of resistance?,ohm|volt|ampere,ohm,\n"
           "11,11.9,,APPLY,SHORT_ANSWER,,,Bad topic,,x,\n"
           "11,,,REMEMBER,MCQ,,,Answer not in options,a|b,c,\n")
    result = await bank.bulk_import_school_dataset_questions(
        own.id, file=UploadFile(filename="q.csv", file=io.BytesIO(csv.encode())), user=s["admin"], session=db_session)
    assert (result.created_count, result.error_count) == (2, 2)
    assert [r.row for r in result.results if r.status == "error"] == [4, 5]
    questions = await bank.list_school_dataset_questions(
        own.id, chapter_id=None, topic_id=None, subtopic_id=str(s["subtopic"].id), bloom_level=None, difficulty=None,
        limit=100, offset=0, user=s["admin"], session=db_session)
    assert questions.total == 1 and questions.items[0].marks == 2


@pytest.mark.asyncio
async def test_disabling_a_subject_for_the_school_switches_off_its_datasets_and_re_enabling_restores_them(db_session):
    from sqlalchemy import select

    s = await _seed(db_session)
    own = await _new_bank(db_session, s)
    school_id = str(s["schools"][0].id)
    await admin_routes.update_datasets_as_admin(
        school_id, UpdateSchoolDatasetsIn(dataset_ids=[str(s["catalog"].id)]), actor=s["super_admin"], session=db_session)
    curriculum = (await db_session.execute(select(SchoolCurriculum).where(
        SchoolCurriculum.school_id == s["schools"][0].id, SchoolCurriculum.subject_id == s["subject"].id))).scalar_one()

    async def usable():
        return {str(d.id) for d in await visible_datasets(db_session, s["teacher"])} & {own.id, str(s["catalog"].id)}

    assert await usable() == {own.id, str(s["catalog"].id)}

    curriculum.enabled = False                     # subject switched off for the school
    await db_session.flush()
    assert await usable() == set()
    listing = await school_admin_routes.get_school_datasets(user=s["admin"], session=db_session)
    assert {(d.id, d.enabled, d.subject_enabled) for d in listing.items} == {
        (own.id, True, False), (str(s["catalog"].id), True, False)}   # still assigned/owned, just inactive

    curriculum.enabled = True                      # back on: nothing had to be re-assigned
    await db_session.flush()
    assert await usable() == {own.id, str(s["catalog"].id)}


@pytest.mark.asyncio
async def test_a_disabled_subjects_bank_stays_readable_but_frozen(db_session):
    from sqlalchemy import update

    from shared.errors import ConflictError

    s = await _seed(db_session)
    created = await _new_bank(db_session, s)
    assert created.subject_enabled and created.subject_name == s["subject"].name
    await db_session.execute(update(SchoolCurriculum).where(SchoolCurriculum.school_id == s["schools"][0].id)
                             .values(enabled=False))
    await db_session.flush()

    listed = await bank.list_school_dataset_questions(created.id, user=s["admin"], session=db_session,
                                                      chapter_id=None, topic_id=None, subtopic_id=None,
                                                      bloom_level=None, difficulty=None, limit=100, offset=0)
    assert listed.total == 0
    with pytest.raises(ConflictError):
        await bank.update_school_dataset(created.id, bank.UpdateSchoolDatasetIn(name="x"), user=s["admin"],
                                         session=db_session)
    with pytest.raises(ValidationError):   # and no new bank for a subject the school doesn't have
        await _new_bank(db_session, s, name="another")
