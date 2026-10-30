import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, true, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .mixins import IdMixin, TimestampMixin


class UserDevice(IdMixin, TimestampMixin, Base):
    """An Expo push token registered by the app (POST /me/devices). A token
    identifies one app install, so it is unique: signing into another
    account on the same phone moves the token to that user."""

    __tablename__ = "user_devices"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    expo_push_token: Mapped[str] = mapped_column(String, unique=True)
    platform: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserNotificationPref(Base):
    """Per-alert push switches (GET/PUT /me/notification-prefs). No row = every alert on.
    The columns are the alert kinds `push.notify(kind=...)` filters by."""

    __tablename__ = "user_notification_prefs"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    results_ready: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    answers_to_review: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    daily_summary: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
