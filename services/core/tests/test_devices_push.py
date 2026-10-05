"""POST/DELETE /me/devices and the Expo push sender (services/push.py).

Real rows against the dev Postgres via db_session. Until the dev DB has
this item's migration, `schema` creates user_devices inside the test's own
transaction without its FK (see test_voice_test_questions.py for why)."""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable

from src.api.routes import devices
from src.domain.models import Role, User, UserStatus
from src.domain.models.device import UserDevice
from src.services import push

REVISION = "e5f1c7a2d9b3"


async def _ddl(db, work, attempts=5):
    for attempt in range(attempts):
        savepoint = await db.begin_nested()
        try:
            await db.execute(text("SET LOCAL lock_timeout = '3s'"))
            await db.run_sync(lambda s: work(s.connection()))
            await savepoint.commit()
            return
        except DBAPIError as exc:
            await savepoint.rollback()
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code not in ("40P01", "55P03") or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


def _create_table(conn):
    if not inspect(conn).has_table("user_devices"):
        conn.execute(CreateTable(UserDevice.__table__, include_foreign_key_constraints=[]))


@pytest_asyncio.fixture
async def schema(db_session):
    await _ddl(db_session, _create_table)
    return db_session


async def _user(db, role=Role.STUDENT) -> User:
    user = User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="U",
                role=role, status=UserStatus.ACTIVE)
    db.add(user)
    await db.flush()
    return user


async def _tokens(db, user):
    rows = await db.execute(select(UserDevice).where(UserDevice.user_id == user.id))
    return {(d.expo_push_token, d.platform) for d in rows.scalars().all()}


def _token():
    return f"ExponentPushToken[{uuid.uuid4().hex}]"


# ------------------------------------------------------------- migration
def _migration_module():
    here = Path(__file__).resolve()
    for root in (here.parents[1], *here.parents[2:4]):
        path = root / "database" / "migrations" / "versions" / f"{REVISION}_user_devices.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("w3_07_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("database/ not available in this environment")


async def test_migration_upgrade_and_downgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _migration_module()
    assert module.down_revision == "c5e7a2b9d413"  # chained after W3-05 at merge

    def run(fn):
        def work(conn):
            with Operations.context(MigrationContext.configure(conn)):
                fn()
        return work

    def columns(sync_session):
        insp = inspect(sync_session.connection())
        if not insp.has_table("user_devices"):
            return None
        return {c["name"] for c in insp.get_columns("user_devices")}

    if await db_session.run_sync(columns) is None:  # dev DB not migrated yet; all rolled back after
        await _ddl(db_session, run(module.upgrade))
    assert await db_session.run_sync(columns) == {c.name for c in UserDevice.__table__.columns}
    await _ddl(db_session, run(module.downgrade))
    assert await db_session.run_sync(columns) is None


# ---------------------------------------------------------------- routes
async def test_register_upserts_and_moves_token_between_users(schema):
    db = schema
    student, teacher = await _user(db), await _user(db, Role.TEACHER)
    token = _token()

    out = await devices.register_device(devices.RegisterDeviceIn(expo_push_token=token, platform="android"),
                                        student, db)
    assert out.model_dump(by_alias=True) == {"expoPushToken": token, "platform": "android"}
    await devices.register_device(devices.RegisterDeviceIn(expoPushToken=token, platform="ios"), student, db)
    assert await _tokens(db, student) == {(token, "ios")}  # same install: updated, not duplicated

    await devices.register_device(devices.RegisterDeviceIn(expoPushToken=token, platform="ios"), teacher, db)
    assert await _tokens(db, student) == set()
    assert await _tokens(db, teacher) == {(token, "ios")}


async def test_unregister_only_removes_own_token(schema):
    db = schema
    owner, other = await _user(db), await _user(db)
    token = _token()
    await devices.register_device(devices.RegisterDeviceIn(expoPushToken=token, platform="android"), owner, db)

    await devices.unregister_device(token, other, db)
    assert await _tokens(db, owner) == {(token, "android")}
    response = await devices.unregister_device(token, owner, db)
    assert response.status_code == 204 and await _tokens(db, owner) == set()
    await devices.unregister_device(token, owner, db)  # idempotent


def test_register_validates_platform_and_token():
    with pytest.raises(ValidationError):
        devices.RegisterDeviceIn(expoPushToken="x", platform="blackberry")
    with pytest.raises(ValidationError):
        devices.RegisterDeviceIn(expoPushToken="", platform="ios")


def test_routes_are_mounted(client):
    paths = {(r.path, m) for r in client.app.routes for m in getattr(r, "methods", ())}
    assert ("/api/v1/me/devices", "POST") in paths
    assert ("/api/v1/me/devices/{token}", "DELETE") in paths


# ------------------------------------------------------------------ push
async def test_send_push_is_off_unless_enabled(schema, monkeypatch):
    db = schema
    user = await _user(db)
    db.add(UserDevice(user_id=user.id, expo_push_token=_token(), platform="ios"))
    await db.flush()
    monkeypatch.delenv("EXPO_PUSH_ENABLED", raising=False)

    async def boom(messages):
        raise AssertionError("must not call Expo")
    monkeypatch.setattr(push, "_post", boom)
    assert await push.send_push(db, [user.id], "Hi", "There") == 0


async def test_send_push_batches_and_prunes_unregistered_tokens(schema, monkeypatch):
    db = schema
    user, other = await _user(db), await _user(db)
    tokens = [_token() for _ in range(push._BATCH + 1)]
    db.add_all([UserDevice(user_id=user.id, expo_push_token=t, platform="android") for t in tokens])
    db.add(UserDevice(user_id=other.id, expo_push_token=_token(), platform="ios"))
    await db.flush()
    monkeypatch.setenv("EXPO_PUSH_ENABLED", "true")
    calls = []

    async def fake(messages):
        calls.append(messages)
        tickets = [{"status": "ok", "id": "x"} for _ in messages]
        if len(calls) == 1:
            tickets[0] = {"status": "error", "details": {"error": "DeviceNotRegistered"}}
        return {"data": tickets}
    monkeypatch.setattr(push, "_post", fake)

    sent = await push.send_push(db, [user.id], "Homework ready", "3 questions", {"homeworkId": "h1"})

    assert sent == len(tokens) and [len(c) for c in calls] == [push._BATCH, 1]
    first = calls[0][0]
    assert (first["title"], first["body"], first["data"]) == ("Homework ready", "3 questions", {"homeworkId": "h1"})
    assert {m["to"] for c in calls for m in c} == set(tokens)  # other user untouched
    assert len(await _tokens(db, user)) == len(tokens) - 1
    assert len(await _tokens(db, other)) == 1


async def test_send_push_swallows_expo_errors(schema, monkeypatch):
    db = schema
    user = await _user(db)
    db.add(UserDevice(user_id=user.id, expo_push_token=_token(), platform="ios"))
    await db.flush()
    monkeypatch.setenv("EXPO_PUSH_ENABLED", "1")

    async def down(messages):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(push, "_post", down)
    assert await push.send_push(db, [user.id], "a", "b") == 0
