"""Question paper template builder (C15): designed templates, scopes
(personal / school / starter) and their authz, list filters, GET one,
PATCH, duplicate, use (incl. starters with a subjectId) and validation.

Same dev-Postgres setup as test_paper_templates.py (its `schema` fixture
and seed)."""

import uuid

import pytest

from shared.errors import ForbiddenError, NotFoundError, ValidationError
from src.api.routes import paper_templates, question_paper
from src.api.schemas.paper_templates import (
    CreatePaperTemplateIn,
    DuplicatePaperTemplateIn,
    UpdatePaperTemplateIn,
    UsePaperTemplateIn,
)
from src.domain.models import QuestionPaperStatus, Subject
from src.services.paper_template_starters import STARTERS
from tests.test_paper_templates import _seed, _template, schema  # noqa: F401 - fixture

STARTER = "starter:cbse-10-half-yearly"


def _design(s, **overrides) -> CreatePaperTemplateIn:
    body = dict(
        name="Weekly quiz", exam_type="Unit Test", board="CBSE", grade=10, subject_id=str(s["subject"].id),
        language="English", total_marks=20, duration_minutes=40,
        sections=[{"name": "A", "questionCount": 10, "marksEach": 1, "questionTypes": ["MCQ"]},
                  {"name": "B", "questionCount": 2, "marksEach": 5, "questionTypes": ["Long Answer"]}],
        bloom_targets=[{"level": "REMEMBER", "percent": 40}, {"level": "APPLY", "percent": 60}],
        difficulty_mix={"easy": 50, "medium": 30, "hard": 20}, chapter_ids=[str(s["ch1"].id)],
    )
    return CreatePaperTemplateIn.model_validate({**body, **overrides})


async def _create(db, s, user="owner", **overrides):
    return await paper_templates.create_template(_design(s, **overrides), user=s[user], session=db)


# ------------------------------------------------------------------ create / get
async def test_design_a_template_from_scratch(schema):
    db = schema
    s = await _seed(db)
    out = await _create(db, s)

    assert (out.name, out.scope, out.exam_type, out.grade, out.total_marks) == (
        "Weekly quiz", "personal", "Unit Test", 10, 20)
    assert [(x.name, x.question_count, x.marks_each, x.question_types) for x in out.sections] == [
        ("A", 10, 1, ["MCQ"]), ("B", 2, 5, ["Long Answer"])]
    assert [(t.level.value, t.percent) for t in out.bloom_targets] == [("REMEMBER", 40), ("APPLY", 60)]
    assert out.difficulty_mix.model_dump() == {"easy": 50, "medium": 30, "hard": 20}
    assert out.chapter_ids == [str(s["ch1"].id)] and (out.can_edit, out.can_delete) == (True, True)

    wire = out.model_dump(by_alias=True, mode="json")
    assert {"examType", "subjectId", "totalMarks", "durationMinutes", "bloomTargets", "difficultyMix",
            "chapterIds", "createdBy", "createdAt", "canEdit", "canDelete", "isStarter"} <= wire.keys()
    assert wire["sections"][0] == {"name": "A", "questionCount": 10, "marksEach": 1, "questionTypes": ["MCQ"]}

    got = await paper_templates.get_template(out.id, user=s["owner"], session=db)
    assert got == out


async def test_design_without_a_subject(schema):
    db = schema
    s = await _seed(db)
    out = await _create(db, s, subject_id=None, chapter_ids=None, bloom_targets=None, difficulty_mix=None)
    assert (out.subject_id, out.chapter_ids, out.bloom_targets, out.difficulty_mix) == (None, [], [], None)


async def test_only_school_admins_create_school_templates(schema):
    db = schema
    s = await _seed(db)
    with pytest.raises(ForbiddenError):
        await _create(db, s, scope="school")
    shared = await _create(db, s, user="admin", scope="school", name="School unit test")
    assert shared.scope == "school"
    seen = await paper_templates.get_template(shared.id, user=s["colleague"], session=db)
    assert (seen.can_edit, seen.can_delete) == (False, False)
    with pytest.raises(NotFoundError):
        await paper_templates.get_template(shared.id, user=s["outsider"], session=db)


@pytest.mark.parametrize("overrides, message", [
    ({"total_marks": 25}, "add up to 20 marks but totalMarks is 25"),
    ({"sections": []}, "at least one section"),
    ({"bloom_targets": [{"level": "APPLY", "percent": 90}]}, "bloomTargets percents must add up to 100"),
    ({"difficulty_mix": {"easy": 50, "medium": 10, "hard": 10}}, "difficultyMix must add up to 100"),
    ({"grade": 13}, "grade must be between 1 and 12"),
    ({"grade": 0}, "grade must be between 1 and 12"),
    ({"sections": [{"name": "A", "questionCount": 20, "marksEach": 1, "questionTypes": ["Essay"]}]},
     'Unknown question type "Essay"'),
    ({"exam_type": None}, "examType"),
    ({"subject_id": None}, "chapterIds need a subjectId"),
])
async def test_create_validation(schema, overrides, message):
    db = schema
    s = await _seed(db)
    with pytest.raises(ValidationError) as exc:
        await _create(db, s, **overrides)
    assert message in exc.value.message


async def test_create_checks_subject_and_chapters(schema):
    db = schema
    s = await _seed(db)
    other_subject = Subject(name=f"Maths-{uuid.uuid4()}", grade=10)
    db.add(other_subject)
    await db.flush()
    with pytest.raises(ValidationError, match="isn't enabled"):
        await _create(db, s, subject_id=str(other_subject.id), chapter_ids=None)
    with pytest.raises(ValidationError, match="another grade"):
        await _create(db, s, grade=9)
    with pytest.raises(ValidationError, match="No subject"):
        await _create(db, s, subject_id="nope", chapter_ids=None)
    with pytest.raises(ValidationError, match="belong to the template's subject"):
        await _create(db, s, chapter_ids=[str(uuid.uuid4())])


# ------------------------------------------------------------------ list
async def test_list_scopes_and_filters(schema):
    db = schema
    s = await _seed(db)
    mine = await _create(db, s, name="Weekly quiz")
    shared = await _create(db, s, user="admin", scope="school", name="Term exam", exam_type="Half-Yearly")
    saved = await _template(db, s, "From paper")

    async def ids(user="owner", **filters):
        return [t.id for t in (await paper_templates.list_templates(user=s[user], session=db, **filters)).items]

    assert set(await ids(scope="personal")) == {mine.id, saved.id}
    assert await ids(scope="school") == [shared.id]
    assert await ids(scope="starter") == list(STARTERS)
    everything = await ids()
    assert set(everything) == {mine.id, shared.id, saved.id, *STARTERS}
    listed = await paper_templates.list_templates(user=s["owner"], session=db)
    assert listed.total == len(listed.items) == 3 + len(STARTERS)

    assert await ids("colleague", scope="personal") == [] and await ids("colleague", scope="school") == [shared.id]
    assert await ids(scope="school", exam_type="Unit Test") == []
    assert set(await ids(exam_type="Half-Yearly")) == {shared.id, "starter:cbse-10-half-yearly",
                                                       "starter:cbse-9-half-yearly"}
    assert await ids(q="weekly") == [mine.id]
    assert await ids(scope="starter", grade=9) == [k for k in STARTERS if "-9-" in k]
    assert await ids(board="ICSE") == []


# ------------------------------------------------------------------ starters
async def test_starters_are_valid_read_only_constants(schema):
    db = schema
    s = await _seed(db)
    assert len(STARTERS) == 6
    for template_id in STARTERS:
        out = await paper_templates.get_template(template_id, user=s["owner"], session=db)
        assert (out.scope, out.is_starter, out.can_edit, out.can_delete, out.subject_id) == (
            "starter", True, False, False, None)
        assert sum(x.question_count * x.marks_each for x in out.sections) == out.total_marks
        assert sum(t.percent for t in out.bloom_targets) == 100
        mix = out.difficulty_mix
        assert mix.easy + mix.medium + mix.hard == 100
    half = await paper_templates.get_template(STARTER, user=s["owner"], session=db)
    assert (half.total_marks, half.duration_minutes, [x.name[-1] for x in half.sections]) == (
        80, 180, list("ABCDE"))
    unit = await paper_templates.get_template("starter:cbse-10-unit-test", user=s["owner"], session=db)
    assert (unit.total_marks, unit.duration_minutes) == (25, 45)

    with pytest.raises(ForbiddenError):
        await paper_templates.update_template(STARTER, UpdatePaperTemplateIn(name="x"),
                                              user=s["admin"], session=db)
    with pytest.raises(ForbiddenError):
        await paper_templates.delete_template(STARTER, user=s["admin"], session=db)
    with pytest.raises(NotFoundError):
        await paper_templates.get_template("starter:nope", user=s["owner"], session=db)
    with pytest.raises(NotFoundError):
        await paper_templates.get_template("not-a-uuid", user=s["owner"], session=db)


# ------------------------------------------------------------------ patch
async def test_patch_by_creator_revalidates_the_whole_blueprint(schema):
    db = schema
    s = await _seed(db)
    out = await _create(db, s)

    patched = await paper_templates.update_template(out.id, UpdatePaperTemplateIn.model_validate({
        "name": "Weekly quiz v2", "durationMinutes": None, "totalMarks": 30,
        "sections": [{"name": "A", "questionCount": 10, "marksEach": 3, "questionTypes": ["MCQ", "TRUE_FALSE"]}],
    }), user=s["owner"], session=db)
    assert (patched.name, patched.total_marks, patched.duration_minutes) == ("Weekly quiz v2", 30, None)
    assert patched.sections[0].question_types == ["MCQ", "TRUE_FALSE"]
    assert patched.bloom_targets == out.bloom_targets and patched.chapter_ids == out.chapter_ids

    with pytest.raises(ValidationError, match="add up to 30 marks but totalMarks is 20"):
        await paper_templates.update_template(out.id, UpdatePaperTemplateIn(total_marks=20),
                                              user=s["owner"], session=db)
    with pytest.raises(ValidationError, match="name is required"):
        await paper_templates.update_template(out.id, UpdatePaperTemplateIn(name=" "), user=s["owner"], session=db)
    cleared = await paper_templates.update_template(out.id, UpdatePaperTemplateIn.model_validate(
        {"bloomTargets": None, "difficultyMix": None, "chapterIds": None, "subjectId": None}),
        user=s["owner"], session=db)
    assert (cleared.bloom_targets, cleared.difficulty_mix, cleared.chapter_ids, cleared.subject_id) == (
        [], None, [], None)


async def test_patch_authz_and_scope_rules(schema):
    db = schema
    s = await _seed(db)
    mine = await _create(db, s)
    with pytest.raises(NotFoundError):  # personal: invisible to others, admins included
        await paper_templates.update_template(mine.id, UpdatePaperTemplateIn(name="x"), user=s["admin"], session=db)
    with pytest.raises(ForbiddenError):  # only school admins share school-wide
        await paper_templates.update_template(mine.id, UpdatePaperTemplateIn(scope="school"),
                                              user=s["owner"], session=db)

    shared = await _create(db, s, user="admin", scope="school")
    with pytest.raises(ForbiddenError):
        await paper_templates.update_template(shared.id, UpdatePaperTemplateIn(name="x"),
                                              user=s["colleague"], session=db)
    renamed = await paper_templates.update_template(shared.id, UpdatePaperTemplateIn(name="Shared quiz"),
                                                    user=s["admin"], session=db)
    assert (renamed.name, renamed.scope) == ("Shared quiz", "school")
    with pytest.raises(NotFoundError):
        await paper_templates.update_template(shared.id, UpdatePaperTemplateIn(name="x"),
                                              user=s["outsider"], session=db)


async def test_patch_a_template_saved_from_a_paper(schema):
    db = schema
    s = await _seed(db)
    out = await _template(db, s)  # sections without per-section types, bloom from the paper
    assert [x.name for x in out.sections] == ["A", "B"] and out.sections[0].question_types == []
    patched = await paper_templates.update_template(out.id, UpdatePaperTemplateIn(duration_minutes=60),
                                                    user=s["owner"], session=db)
    assert (patched.duration_minutes, patched.total_marks) == (60, 20)

    used = await paper_templates.use_template(out.id, UsePaperTemplateIn(name="Again"), user=s["owner"], session=db)
    paper = await question_paper.get_paper(used.paper_id, user=s["owner"], session=db)
    assert (paper.duration_minutes, paper.question_types, paper.topic_ids, paper.instructions) == (
        60, ["MCQ", "SHORT"], [str(s["topic"].id)], ["All questions are compulsory."])


# ------------------------------------------------------------------ duplicate
async def test_duplicate_makes_a_personal_copy(schema):
    db = schema
    s = await _seed(db)
    shared = await _create(db, s, user="admin", scope="school", name="Term exam")

    copy = await paper_templates.duplicate_template(shared.id, DuplicatePaperTemplateIn(),
                                                    user=s["colleague"], session=db)
    assert (copy.name, copy.scope, copy.created_by, copy.can_edit) == (
        "Term exam (copy)", "personal", str(s["colleague"].id), True)
    assert copy.id != shared.id and copy.sections == shared.sections and copy.subject_id == shared.subject_id

    starter = await paper_templates.duplicate_template(STARTER, DuplicatePaperTemplateIn(name="My half-yearly"),
                                                       user=s["owner"], session=db)
    assert (starter.name, starter.scope, starter.is_starter, starter.total_marks) == (
        "My half-yearly", "personal", False, 80)
    edited = await paper_templates.update_template(starter.id, UpdatePaperTemplateIn(
        subject_id=str(s["subject"].id)), user=s["owner"], session=db)
    assert edited.subject_id == str(s["subject"].id)

    with pytest.raises(NotFoundError):
        await paper_templates.duplicate_template(copy.id, DuplicatePaperTemplateIn(), user=s["owner"], session=db)


# ------------------------------------------------------------------ use
async def test_use_a_designed_template(schema):
    db = schema
    s = await _seed(db)
    out = await _create(db, s)
    used = await paper_templates.use_template(out.id, UsePaperTemplateIn(name="Quiz 1"), user=s["owner"], session=db)
    paper = await question_paper.get_paper(used.paper_id, user=s["owner"], session=db)
    assert (paper.name, paper.status, paper.subject_id, paper.total_marks, paper.duration_minutes) == (
        "Quiz 1", QuestionPaperStatus.DRAFT, str(s["subject"].id), 20, 40)
    assert [(x.label, x.question_count, x.marks_each) for x in paper.sections] == [("A", 10, 1), ("B", 2, 5)]
    assert paper.question_types == ["MCQ", "Long Answer"] and paper.chapter_ids == [str(s["ch1"].id)]
    assert sorted((t.level.value, t.percent) for t in paper.bloom_targets) == [("APPLY", 60), ("REMEMBER", 40)]
    assert paper.difficulty_mix.model_dump() == {"easy": 50, "medium": 30, "hard": 20}


async def test_use_a_starter_needs_a_subject(schema):
    db = schema
    s = await _seed(db)
    with pytest.raises(ValidationError, match="subjectId"):
        await paper_templates.use_template(STARTER, UsePaperTemplateIn(name="HY"), user=s["owner"], session=db)

    used = await paper_templates.use_template(STARTER, UsePaperTemplateIn(
        name="Half-yearly", subject_id=str(s["subject"].id), chapter_ids=[str(s["ch1"].id), str(s["ch2"].id)]),
        user=s["owner"], session=db)
    paper = await question_paper.get_paper(used.paper_id, user=s["owner"], session=db)
    assert (paper.exam_type, paper.total_marks, paper.duration_minutes, len(paper.sections)) == (
        "Half-Yearly", 80, 180, 5)
    assert set(paper.chapter_ids) == {str(s["ch1"].id), str(s["ch2"].id)}

    with pytest.raises(ForbiddenError):  # grade 9 starter, the teacher teaches grade 10
        await paper_templates.use_template("starter:cbse-9-unit-test", UsePaperTemplateIn(
            name="x", subject_id=str(s["subject"].id)), user=s["owner"], session=db)


async def test_use_with_another_subject_drops_the_templates_chapters(schema):
    db = schema
    s = await _seed(db)
    out = await _create(db, s, subject_id=None, chapter_ids=None)
    used = await paper_templates.use_template(out.id, UsePaperTemplateIn(
        name="Quiz", subject_id=str(s["subject"].id)), user=s["owner"], session=db)
    paper = await question_paper.get_paper(used.paper_id, user=s["owner"], session=db)
    assert (paper.subject_id, paper.chapter_ids) == (str(s["subject"].id), [])


# ------------------------------------------------------------------ wiring
def test_routes_are_registered():
    from src.main import create_app

    spec = create_app().openapi()
    base = "/api/v1/question-paper-templates"
    assert {"get", "post"} <= spec["paths"][base].keys()
    assert {"get", "patch", "delete"} <= spec["paths"][base + "/{template_id}"].keys()
    assert "post" in spec["paths"][base + "/{template_id}/duplicate"]
    params = {p["name"] for p in spec["paths"][base]["get"]["parameters"]}
    assert {"scope", "board", "grade", "examType", "q"} <= params


async def test_over_http_camel_case_query_and_body(schema):
    """Through the ASGI app in this test's own loop (not TestClient), so the
    examType query alias and the camelCase wire shape are exercised."""
    import httpx

    from src.api.deps import get_current_app_user, get_db_session
    from src.main import create_app

    db = schema
    s = await _seed(db)
    app = create_app()

    async def _session():
        yield db

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_app_user] = lambda: s["owner"]
    base = "/api/v1/question-paper-templates"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(base, json={
            "name": "100%_quiz", "examType": "Unit Test", "board": "CBSE", "grade": 10,
            "subjectId": str(s["subject"].id), "language": "English", "totalMarks": 10, "durationMinutes": 30,
            "sections": [{"name": "A", "questionCount": 10, "marksEach": 1, "questionTypes": ["MCQ"]}]})
        assert created.status_code == 201, created.text
        body = created.json()
        assert (body["examType"], body["totalMarks"], body["scope"], body["canEdit"]) == (
            "Unit Test", 10, "personal", True)

        listed = (await client.get(base, params={"scope": "personal", "examType": "Unit Test"})).json()
        assert body["id"] in [t["id"] for t in listed["items"]] and listed["total"] == len(listed["items"])
        other = (await client.get(base, params={"scope": "personal", "examType": "Pre-Board"})).json()
        assert body["id"] not in [t["id"] for t in other["items"]]

        # % and _ in a search are literal, not wildcards
        hit = (await client.get(base, params={"scope": "personal", "q": "100%_"})).json()
        assert [t["id"] for t in hit["items"]] == [body["id"]]
        miss = (await client.get(base, params={"scope": "personal", "q": "1%z"})).json()
        assert miss["items"] == []

        bad = await client.patch(f"{base}/{body['id']}", json={"totalMarks": 12})
        assert bad.status_code == 422 and "totalMarks" in bad.text


async def test_patch_keeps_an_old_uneven_distribution_unless_it_changes(schema):
    from src.domain.models.paper_template import QuestionPaperTemplate

    db = schema
    s = await _seed(db)
    out = await _create(db, s)
    row = await db.get(QuestionPaperTemplate, uuid.UUID(out.id))
    # as saved from an older paper whose distributions did not add up to 100
    row.blueprint = {**row.blueprint, "bloom_targets": [{"level": "REMEMBER", "percent": 90}],
                     "difficulty_mix": {"easy": 30, "medium": 30, "hard": 30}}
    await db.flush()

    edited = await paper_templates.update_template(out.id, UpdatePaperTemplateIn(duration_minutes=50),
                                                   user=s["owner"], session=db)
    assert edited.duration_minutes == 50 and [t.percent for t in edited.bloom_targets] == [90]

    with pytest.raises(ValidationError, match="bloomTargets"):
        await paper_templates.update_template(out.id, UpdatePaperTemplateIn.model_validate(
            {"bloomTargets": [{"level": "APPLY", "percent": 80}]}), user=s["owner"], session=db)
    with pytest.raises(ValidationError, match="difficultyMix"):
        await paper_templates.update_template(out.id, UpdatePaperTemplateIn.model_validate(
            {"difficultyMix": {"easy": 10, "medium": 10, "hard": 10}}), user=s["owner"], session=db)
