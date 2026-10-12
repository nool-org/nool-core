from collections.abc import AsyncIterator

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from shared.auth import AuthenticatedUser, verify_token
from shared.errors import ForbiddenError, UnauthorizedError

from src.domain.models import Feature, Role, School, SchoolStatus, Subscription, SubscriptionStatus, User
from src.repositories import get_by_firebase_uid, get_session
from src.repositories.subscription_repository import get_active_plan
from src.services import ai_context
from src.services.feature_entitlements import is_feature_enabled

# A subscription in either of these states means the school's billing
# relationship has ended (or never resolved) - access is blocked the same
# way a SUSPENDED school is, not just "treated as unlimited" the way "no
# subscription row yet" (a mid-onboarding school) already correctly is.
_BLOCKED_SUBSCRIPTION_STATUSES = (SubscriptionStatus.CANCELED, SubscriptionStatus.PAST_DUE)


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header.")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("Missing or malformed Authorization header.")
    return token


def get_current_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    """Authentication middleware foundation for Core.

    Core verifies tokens independently rather than calling the Auth
    service per-request - tokens are self-contained, so any service can
    verify them via the same shared.auth IdentityProvider seam the Auth
    service uses (see shared/auth/provider.py). The provider itself is
    constructed and registered once, at app startup (src/main.py's
    create_app() - the one place in Core that names FirebaseIdentityProvider
    concretely), not here - see token_service.py's docstring in the Auth
    service for why per-request construction is unsafe.
    """
    token = _extract_bearer_token(authorization)
    return verify_token(token)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-compatible wrapper around repositories.get_session's
    @asynccontextmanager (which is written for plain ``async with`` use,
    not as a raw FastAPI dependency).
    """
    async with get_session() as session:
        yield session


async def get_current_app_user(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Resolves the verified Firebase identity to Core's own `users` row -
    the actual authorization-relevant identity (role, school_id), which a
    bare Firebase custom claim can't carry (no school_id at all). Raises
    UnauthorizedError if the token is valid but no application user exists
    yet - every route except GET /api/v1/me itself requires a resolved
    profile, so this is the right layer to reject at (mirrors the doc's
    "valid token, no profile" state, which only /me surfaces as data).
    """
    app_user = await get_by_firebase_uid(session, user.uid)
    if app_user is None:
        raise UnauthorizedError("No application profile exists for this account yet.")
    if app_user.school_id is not None:
        result = await session.execute(
            select(School.status, Subscription.status)
            .outerjoin(Subscription, Subscription.school_id == School.id)
            .where(School.id == app_user.school_id)
        )
        row = result.first()
        # A school row always exists for a user with school_id set (FK-
        # enforced) - row is None only if that invariant is somehow
        # violated, in which case failing open here is wrong, but there's
        # genuinely nothing to check against.
        if row is not None:
            school_status, subscription_status = row
            if school_status == SchoolStatus.SUSPENDED:
                raise ForbiddenError(
                    "This school's access has been suspended. Contact your platform administrator."
                )
            # subscription_status is None for a school with no Subscription
            # row yet (mid-onboarding) - that's the existing, correct
            # "not yet a billable context" grace period (see
            # get_active_plan's docstring), not a block.
            if subscription_status in _BLOCKED_SUBSCRIPTION_STATUSES:
                raise ForbiddenError(
                    "This school's subscription is no longer active. Contact your platform administrator."
                )
    ai_context.set_caller(app_user)  # labels this request's AI calls for the cost logs
    return app_user


def require_role(*roles: Role):
    """FastAPI dependency factory: 403s unless the caller's resolved role
    is one of `roles`. Route-level scoping (Teacher -> own school, Student
    -> /me/*, Super Admin -> cross-tenant, School Admin -> own school) is
    layered on top of this in each router, not here - this only answers
    "is this role allowed to call this route at all."
    """

    async def _check(user: User = Depends(get_current_app_user)) -> User:
        if user.role not in roles:
            raise ForbiddenError("Your role does not permit this action.")
        return user

    return _check


def require_feature(feature: Feature):
    """FastAPI dependency factory: 403s if `feature` is excluded by the
    caller's school's active plan/subscription - the single source of
    truth for feature access (see src/services/feature_entitlements.py
    and Plan.enabled_features' docstring: deliberately not also settable
    per-school, one control point). NULL means "no restriction," so this
    is a no-op for every plan that hasn't been narrowed by a Super Admin.
    A caller with no school_id (Super Admin) is never blocked.
    """

    async def _check(
        user: User = Depends(get_current_app_user),
        session: AsyncSession = Depends(get_db_session),
    ) -> User:
        if user.school_id is None:
            return user
        plan = await get_active_plan(session, user.school_id)
        if not is_feature_enabled(feature, plan=plan):
            raise ForbiddenError(f'The "{feature.value}" feature is not enabled for your school\'s plan.')
        return user

    return _check
