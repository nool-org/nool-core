"""Expo push sender. Expo's push API needs no credentials; sending is off
unless CoreSettings.expo_push_enabled (EXPO_PUSH_ENABLED) is true, so tests
and local dev never call out.

Triggers call `await notify(session, ...)` right after their own commit.
"""

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.logging import get_logger
from src.config.settings import get_settings
from src.domain.models.device import UserDevice

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_BATCH = 100  # Expo's per-request message limit
logger = get_logger(__name__)


def push_enabled() -> bool:
    return get_settings().expo_push_enabled


async def _post(messages: list[dict]) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(EXPO_PUSH_URL, json=messages, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()


async def send_push(session: AsyncSession, user_ids, title: str, body: str, data: dict | None = None) -> int:
    """Sends one notification to every registered device of `user_ids`.
    Returns the number of messages handed to Expo (0 when disabled).
    Never raises on delivery problems - a push is best-effort. Tokens Expo
    reports as DeviceNotRegistered are deleted; caller commits."""
    if not push_enabled() or not user_ids:
        return 0
    tokens = list((await session.execute(
        select(UserDevice.expo_push_token).where(UserDevice.user_id.in_(list(user_ids)))
    )).scalars().all())
    sent, dead = 0, []
    for start in range(0, len(tokens), _BATCH):
        batch = tokens[start:start + _BATCH]
        messages = [{"to": t, "title": title, "body": body, "data": data or {}, "sound": "default"} for t in batch]
        try:
            result = await _post(messages)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("push_send_failed", extra={"error": str(exc), "count": len(batch)})
            continue
        sent += len(batch)
        for token, ticket in zip(batch, result.get("data") or []):
            if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                dead.append(token)
    if dead:
        await session.execute(delete(UserDevice).where(UserDevice.expo_push_token.in_(dead)))
    return sent


async def notify(session: AsyncSession, user_ids, title: str, body: str, data: dict | None = None) -> None:
    """send_push for a trigger, called after the trigger's own commit: commits
    the pruned tokens, and logs instead of raising - a push never fails the
    request that caused it.
    ponytail: inline, so a slow Expo delays the response (10s timeout per
    batch); move to a background task if that bites."""
    try:
        async with session.begin_nested():  # a failure rolls back only this, not the caller's loaded objects
            await send_push(session, user_ids, title, body, data)
        await session.commit()
    except Exception as exc:
        logger.warning("push_notify_failed", extra={"error": str(exc), "title": title})
