from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth import AuthenticatedUser
from shared.errors import UnauthorizedError, ValidationError

from src.api.deps import get_current_app_user, get_current_user, get_db_session
from src.api.schemas.profile import AvatarIn, MeResponse, ProfileOut
from src.domain.models import School, User
from src.repositories import get_by_firebase_uid
from src.repositories.subscription_repository import get_active_plan
from src.services.avatars import AVATARS
from src.services.feature_entitlements import effective_enabled_features
from src.services.user_provisioning import clear_must_change_password_claim

router = APIRouter(prefix="/api/v1", tags=["profile"])


@router.get("/me", summary="Resolve the signed-in profile")
async def get_me(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    """Called immediately after Firebase sign-in. `profile: null` means
    the token is genuinely valid but has no matching application user
    yet - a real, expected "unknown profile" state, not an error (see
    src/api/deps.py's get_current_app_user, which every *other* route
    uses and which does treat this as a 401 - this route is the one
    exception, by design).
    """
    app_user = await get_by_firebase_uid(session, user.uid)
    if app_user is None:
        return MeResponse(profile=None)

    school_name = ""
    school_logo_data_uri = None
    plan = None
    if app_user.school_id is not None:
        result = await session.execute(
            select(School.name, School.logo_data_uri).where(School.id == app_user.school_id)
        )
        row = result.one_or_none()
        if row is not None:
            school_name, school_logo_data_uri = row
        plan = await get_active_plan(session, app_user.school_id)

    return MeResponse(
        profile=ProfileOut(
            id=str(app_user.id),
            role=app_user.role,
            display_name=app_user.display_name,
            school=school_name,
            school_logo_data_uri=school_logo_data_uri,
            must_change_password=app_user.must_change_password,
            enabled_features=effective_enabled_features(plan=plan),
            avatar=app_user.avatar,
        )
    )


@router.put("/me/avatar", summary="Pick your picture")
async def set_my_avatar(
    body: AvatarIn,
    user: User = Depends(get_current_app_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str | None]:
    """One of the app's avatars, or null for initials - for yourself only."""
    if body.avatar is not None and body.avatar not in AVATARS:
        raise ValidationError("Pick one of the avatars shown.")
    user.avatar = body.avatar
    await session.commit()
    return {"avatar": user.avatar}


@router.post("/me/acknowledge-password-change")
async def acknowledge_password_change(
    user: User = Depends(get_current_app_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, bool]:
    """Called once the client has successfully changed the account's
    temp password via Firebase's own client-side updatePassword - clears
    both the DB flag and the mirrored Firebase custom claim (see
    src/services/user_provisioning.py). No request body: the caller is
    always acknowledging for themselves, never another user.
    """
    if user.firebase_uid is None:
        raise UnauthorizedError("No Firebase account is linked to this profile.")
    user.must_change_password = False
    await session.commit()
    clear_must_change_password_claim(firebase_uid=user.firebase_uid, role=user.role)
    return {"mustChangePassword": False}
