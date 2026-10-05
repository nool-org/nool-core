"""Expo push tokens per user (C8). Sending lives in src/services/push.py."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import Field
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_app_user, get_db_session
from src.api.schemas.common import CamelModel
from src.domain.models import User
from src.domain.models.device import UserDevice

router = APIRouter(prefix="/api/v1", tags=["devices"])


class RegisterDeviceIn(CamelModel):
    expo_push_token: str = Field(min_length=1, max_length=255)
    platform: Literal["ios", "android", "web"]


class DeviceOut(CamelModel):
    expo_push_token: str
    platform: str


@router.post("/me/devices", summary="Register (or move to this user) an Expo push token")
async def register_device(
    body: RegisterDeviceIn,
    user: User = Depends(get_current_app_user),
    session: AsyncSession = Depends(get_db_session),
) -> DeviceOut:
    now = datetime.now(timezone.utc)
    stmt = insert(UserDevice).values(
        expo_push_token=body.expo_push_token, platform=body.platform, user_id=user.id, updated_at=now
    )
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[UserDevice.expo_push_token],
        set_={"user_id": user.id, "platform": body.platform, "updated_at": now},
    ))
    await session.commit()
    return DeviceOut(expo_push_token=body.expo_push_token, platform=body.platform)


@router.delete("/me/devices/{token}", status_code=204, summary="Unregister one of my push tokens")
async def unregister_device(
    token: str,
    user: User = Depends(get_current_app_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    # Idempotent: deleting an unknown (or someone else's) token is a no-op.
    await session.execute(
        delete(UserDevice).where(UserDevice.expo_push_token == token, UserDevice.user_id == user.id)
    )
    await session.commit()
    return Response(status_code=204)
