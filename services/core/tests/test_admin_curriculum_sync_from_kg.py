"""POST /admin/curriculum/sync-from-kg runs once per Dataset that names a
real KG root (board+grade+subject_id all set) - a Dataset like "10th
Science" *is* that root (see Dataset's docstring), so two datasets sharing
one Subject at different grades (e.g. "9th Science" and "10th Science")
must sync into separately-graded Chapter rows, never merging into one
flat, ungraded list.
"""

import uuid

import pytest
from sqlalchemy import select

from src.api.routes import admin_catalog
from src.domain.models import Chapter, Dataset, DatasetType, Role, Subject, User, UserStatus


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


@pytest.mark.asyncio
async def test_each_grades_subject_syncs_its_own_chapters(
    db_session, monkeypatch,
):
    """Subjects are per grade: "Science" 9 and "Science" 10 are two subjects,
    each syncing its own grade's chapters from its own dataset."""
    actor = await _seed_super_admin(db_session)
    name = f"Science-{uuid.uuid4()}"
    subject_9, subject_10 = Subject(name=name, grade=9), Subject(name=name, grade=10)
    db_session.add_all([subject_9, subject_10])
    await db_session.flush()

    ninth = Dataset(
        name="9th Science", question_count=0, description="", subject_id=subject_9.id, board="CBSE", grade=9,
        type=DatasetType.PRIMARY_CONTENT,
    )
    tenth = Dataset(
        name="10th Science", question_count=0, description="", subject_id=subject_10.id, board="CBSE", grade=10,
        type=DatasetType.PRIMARY_CONTENT,
    )
    db_session.add_all([ninth, tenth])
    await db_session.flush()

    ref_9, ref_10 = f"kg-9-{uuid.uuid4()}", f"kg-10-{uuid.uuid4()}"
    own_subject_name = name

    async def fake_get_curriculum_tree(*, board, grade, subject):
        # Keyed on this test's own subject name too, not just grade - the
        # real catalog may (and does) already have its own grade-10
        # dataset under a different subject syncing alongside this one.
        if subject != own_subject_name:
            return {"subject": subject, "chapters": []}
        ref = ref_9 if grade == 9 else ref_10
        return {
            "subject": subject,
            "chapters": [{"ref": ref, "number": 1, "name": f"Chapter for grade {grade}", "topics": []}],
        }

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree", fake_get_curriculum_tree
    )

    result = await admin_catalog.sync_curriculum_from_kg(actor=actor, session=db_session)

    # Scoped to this test's own two datasets - the real catalog may (and,
    # in a shared dev database, does) already have other qualifying
    # datasets that legitimately sync alongside these.
    own_results = {r.dataset_id: r for r in result.results if r.dataset_id in {str(ninth.id), str(tenth.id)}}
    assert len(own_results) == 2
    assert {r.grade for r in own_results.values()} == {9, 10}

    chapter_9 = (await db_session.execute(select(Chapter).where(Chapter.kg_ref == ref_9))).scalar_one()
    chapter_10 = (await db_session.execute(select(Chapter).where(Chapter.kg_ref == ref_10))).scalar_one()

    assert chapter_9.grade == 9
    assert chapter_10.grade == 10
    assert chapter_9.subject_id == subject_9.id
    assert chapter_10.subject_id == subject_10.id


@pytest.mark.asyncio
async def test_a_dataset_missing_board_or_grade_is_skipped(db_session, monkeypatch):
    actor = await _seed_super_admin(db_session)
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    db_session.add(subject)
    await db_session.flush()

    incomplete = Dataset(
        name="No KG root yet", question_count=0, description="", subject_id=subject.id, board=None, grade=None,
    )
    db_session.add(incomplete)
    await db_session.flush()

    queried_subjects: list[str] = []

    async def fake_get_curriculum_tree(*, board, grade, subject):
        queried_subjects.append(subject)
        return {"subject": subject, "chapters": []}

    monkeypatch.setattr(
        "src.api.routes.admin_catalog.kg_client.get_curriculum_tree", fake_get_curriculum_tree
    )

    result = await admin_catalog.sync_curriculum_from_kg(actor=actor, session=db_session)

    # This test's own incomplete dataset never appears in the results, and
    # the kg service is never queried for its subject - other qualifying
    # datasets in the (shared) catalog may still legitimately sync.
    assert not any(r.dataset_id == str(incomplete.id) for r in result.results)
    assert subject.name not in queried_subjects
