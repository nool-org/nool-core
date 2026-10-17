"""School logo (base64 data URI on School, same precedent as
QuestionPaper.logo_data_uri) - both the Super Admin and School Admin
self-service endpoints, and its exposure on GET /api/v1/me. Real rows
against the dev Postgres via db_session.
"""

import uuid

from shared.auth import AuthenticatedUser

from src.api.routes import admin, profile, school_admin
from src.api.schemas.school_logo import UpdateSchoolLogoIn
from src.domain.models import Role, School, User, UserStatus

_SAMPLE_LOGO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


async def _seed_school(db_session) -> School:
    school = School(name="Test School", board="CBSE", city="Bengaluru", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    return school


async def _seed_super_admin(db_session) -> User:
    super_admin = User(
        firebase_uid=f"super-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Super", role=Role.SUPER_ADMIN, school_id=None, status=UserStatus.ACTIVE,
    )
    db_session.add(super_admin)
    await db_session.flush()
    return super_admin


async def _seed_school_admin(db_session, school_id) -> User:
    school_admin_user = User(
        firebase_uid=f"admin-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Admin", role=Role.SCHOOL_ADMIN, school_id=school_id, status=UserStatus.ACTIVE,
    )
    db_session.add(school_admin_user)
    await db_session.flush()
    return school_admin_user


async def test_admin_get_school_logo_defaults_to_none(db_session):
    school = await _seed_school(db_session)
    super_admin = await _seed_super_admin(db_session)

    out = await admin.get_school_logo(str(school.id), _=super_admin, session=db_session)

    assert out.logo_data_uri is None


async def test_admin_can_set_and_clear_a_schools_logo(db_session):
    school = await _seed_school(db_session)
    super_admin = await _seed_super_admin(db_session)

    set_out = await admin.update_school_logo(
        str(school.id), UpdateSchoolLogoIn(logo_data_uri=_SAMPLE_LOGO), actor=super_admin, session=db_session,
    )
    assert set_out.logo_data_uri == _SAMPLE_LOGO
    await db_session.refresh(school)
    assert school.logo_data_uri == _SAMPLE_LOGO

    clear_out = await admin.update_school_logo(
        str(school.id), UpdateSchoolLogoIn(logo_data_uri=None), actor=super_admin, session=db_session,
    )
    assert clear_out.logo_data_uri is None


async def test_school_admin_can_set_their_own_schools_logo(db_session):
    school = await _seed_school(db_session)
    admin_user = await _seed_school_admin(db_session, school.id)

    out = await school_admin.update_school_logo(
        UpdateSchoolLogoIn(logo_data_uri=_SAMPLE_LOGO), user=admin_user, session=db_session,
    )

    assert out.logo_data_uri == _SAMPLE_LOGO

    fetched = await school_admin.get_school_logo(user=admin_user, session=db_session)
    assert fetched.logo_data_uri == _SAMPLE_LOGO


async def test_get_me_includes_the_schools_logo(db_session):
    school = await _seed_school(db_session)
    school.logo_data_uri = _SAMPLE_LOGO
    await db_session.flush()
    teacher = User(
        firebase_uid=f"teacher-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com",
        display_name="Teacher", role=Role.TEACHER, school_id=school.id, status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()

    response = await profile.get_me(
        user=AuthenticatedUser(uid=teacher.firebase_uid, email=teacher.email, claims={}), session=db_session,
    )

    assert response.profile is not None
    assert response.profile.school_logo_data_uri == _SAMPLE_LOGO


def test_logo_must_be_an_uploaded_image():
    import pytest
    from pydantic import ValidationError
    for bad in ("https://tracker.example/logo.png", "javascript:alert(1)", "data:text/html;base64,PGI+"):
        with pytest.raises(ValidationError):
            UpdateSchoolLogoIn(logo_data_uri=bad)
