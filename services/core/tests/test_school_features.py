"""Per-school module switches: a plan bundles modules, a Super Admin switches
some off per school - a school gets a module only when both allow it."""
import uuid

import pytest

from shared.errors import ForbiddenError
from src.api.deps import require_feature
from src.api.routes import admin
from src.api.schemas.features import UpdateSchoolFeaturesIn
from src.domain.models import Feature, Plan, Role, School, User, UserStatus
from src.services.feature_entitlements import effective_enabled_features
from tests.test_admin_students import _seed_school


def test_a_school_gets_what_its_plan_bundles_minus_what_it_switched_off():
    everything, narrowed = Plan(enabled_features=None), Plan(enabled_features=["voice_test", "homework"])
    assert effective_enabled_features(plan=everything, school=School(disabled_features=None)) is None
    assert Feature.TOPIC_QUIZ not in effective_enabled_features(
        plan=everything, school=School(disabled_features=["topic_quiz"]))
    assert effective_enabled_features(plan=narrowed, school=School(disabled_features=["homework"])) == [
        Feature.VOICE_TEST]


async def test_super_admin_switches_a_module_off_for_one_school(db_session):
    school = await _seed_school(db_session)
    actor = User(firebase_uid=f"sa-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="SA",
                 role=Role.SUPER_ADMIN, status=UserStatus.ACTIVE)
    db_session.add(actor)
    await db_session.flush()
    out = await admin.update_school_features(str(school.id), UpdateSchoolFeaturesIn(
        disabled_features=[Feature.GROWTH_CHECK]), actor=actor, session=db_session)
    assert out.disabled_features == [Feature.GROWTH_CHECK] and Feature.GROWTH_CHECK not in out.enabled_features

    student = type("U", (), {"school_id": school.id, "role": Role.STUDENT})()
    with pytest.raises(ForbiddenError):
        await require_feature(Feature.GROWTH_CHECK)(user=student, session=db_session)
    assert await require_feature(Feature.TOPIC_QUIZ)(user=student, session=db_session) is student
