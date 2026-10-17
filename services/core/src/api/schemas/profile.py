from src.domain.models import Feature, Role

from .common import CamelModel


class ProfileOut(CamelModel):
    id: str
    role: Role
    display_name: str
    school: str
    # None = no logo set - see School.logo_data_uri's docstring.
    school_logo_data_uri: str | None
    # For clients that call this DB-backed /me directly (nool-school-admin,
    # nool-super-admin) - nool-apps instead reads the mirrored Firebase
    # custom claim, since it calls Auth's claim-only /me, which has no DB
    # access by design. Both read the same users.must_change_password
    # column - see src/services/user_provisioning.py.
    must_change_password: bool
    # None = every Feature enabled - the school's active plan's allow-list,
    # the single source of truth for feature access (see
    # src/services/feature_entitlements.py). nool-apps now calls this
    # endpoint (in addition to Auth's claim-only /me) specifically to read
    # this field - see services/api/meClient.ts's getMyProfile().
    enabled_features: list[Feature] | None
    # the school's plan name ("Core", "Plus", "Pro"; None = no subscription yet) and its
    # extra challenges a month - the create screens let a taken chapter be picked again when > 0
    plan_name: str | None = None
    extra_challenges_per_month: int = 0
    # the picture they picked (src/services/avatars.py), None = initials
    avatar: str | None = None


class AvatarIn(CamelModel):
    avatar: str | None


class MeResponse(CamelModel):
    """profile: None means the token is genuinely valid but has no
    matching application user yet - a real, expected "unknown profile"
    state, not an error. See src/api/routes/profile.py.
    """

    profile: ProfileOut | None
