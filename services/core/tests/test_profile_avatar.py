"""PUT /me/avatar: one of the app's pictures (or none) - shown in /me and the leaderboard."""

import uuid

import pytest

from shared.errors import ValidationError
from src.api.routes import profile
from src.api.schemas.profile import AvatarIn
from src.domain.models import Role, School, User, UserStatus


async def test_a_user_picks_one_of_the_avatars_and_only_those(db_session):
    school = School(name=f"S-{uuid.uuid4()}", board="CBSE", city="Chennai", contact_email="a@b.com")
    db_session.add(school)
    await db_session.flush()
    user = User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="Asha",
                role=Role.STUDENT, school_id=school.id, status=UserStatus.ACTIVE)
    db_session.add(user)
    await db_session.flush()

    assert await profile.set_my_avatar(AvatarIn(avatar="fox"), user=user, session=db_session) == {"avatar": "fox"}
    assert user.avatar == "fox"
    with pytest.raises(ValidationError):
        await profile.set_my_avatar(AvatarIn(avatar="../../etc"), user=user, session=db_session)
    assert (await profile.set_my_avatar(AvatarIn(avatar=None), user=user, session=db_session)) == {"avatar": None}
