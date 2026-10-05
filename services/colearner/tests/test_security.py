"""Audit fixes: auth on homework-insight, session ownership, input size
caps, idle session expiry, and PLAYGROUND_ENABLED
being ignored in production. verify_token is faked to treat the bearer
token as the uid, so "Bearer alice" is user alice."""

from datetime import datetime, timedelta

import pytest

from shared.auth import AuthenticatedUser

from src.domain.schemas import SessionConfig
from src.services import homework_insight_service
from src.services.session_store import SessionStore, session_store

INSIGHT_BODY = {
    "topic": "Electricity",
    "grade": "Grade 10",
    "subject": "Science",
    "mastery_percent": 40,
    "weak_bloom_level": "APPLY",
}

TURN_RESULT = {"student_transcript": "hi", "text": "reply", "audio": b"", "completed": False, "timing": {}}


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: AuthenticatedUser(uid=token, email=None, claims={}))
    return lambda uid: {"Authorization": f"Bearer {uid}"}


# --- homework-insight auth ---------------------------------------------------


def test_homework_insight_requires_auth(client):
    assert client.post("/api/v1/homework-insight", json=INSIGHT_BODY).status_code == 401


def test_homework_insight_with_auth_returns_fallback(client, as_user, monkeypatch):
    monkeypatch.setattr(homework_insight_service, "openai_client", None)
    res = client.post("/api/v1/homework-insight", json=INSIGHT_BODY, headers=as_user("alice"))
    assert res.status_code == 200
    assert res.json()["key_idea_title"] == "Electricity"


# --- session ownership -------------------------------------------------------


def test_other_users_session_is_404(client, as_user):
    session = session_store.create_session(SessionConfig(), owner_uid="alice")
    res = client.post(
        "/api/v1/colearner/cascaded",
        json={"session_id": session.session_id, "user_text": "hi"},
        headers=as_user("bob"),
    )
    assert res.status_code == 404


def test_owner_can_continue_own_session(client, as_user, monkeypatch):
    async def fake_turn(*_args):
        return TURN_RESULT

    monkeypatch.setattr("src.api.routes.cascaded.process_cascaded_turn", fake_turn)
    first = client.post("/api/v1/colearner/cascaded", json={}, headers=as_user("alice"))
    assert first.status_code == 200
    sid = first.json()["session_id"]
    assert session_store.get_session(sid).owner_uid == "alice"

    again = client.post("/api/v1/colearner/cascaded", json={"session_id": sid, "user_text": "x"}, headers=as_user("alice"))
    assert again.status_code == 200


def test_store_owner_check():
    store = SessionStore()
    s = store.create_session(SessionConfig(), owner_uid="alice")
    assert store.get_session(s.session_id, owner_uid="alice") is s
    assert store.get_session(s.session_id, owner_uid="bob") is None
    # No owner given (realtime_service's path) - unchanged behaviour.
    unowned = store.create_session(SessionConfig())
    assert store.get_session(unowned.session_id) is unowned
    assert store.get_session(unowned.session_id, owner_uid="alice") is None


# --- size caps ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"user_text": "x" * 4001},
        {"user_audio_base64": "A" * 7_000_001},
        {"config": {"textbook_context": "x" * 20_001}},
        {"config": {"reference_questions": ["q"] * 21}},
        {"config": {"reference_questions": ["x" * 501]}},
        {"config": {"chapter_name": "x" * 201}},
    ],
)
def test_oversize_rest_input_rejected(client, as_user, body):
    assert client.post("/api/v1/colearner/cascaded", json=body, headers=as_user("alice")).status_code == 422


def test_oversize_homework_insight_rejected(client, as_user):
    body = {**INSIGHT_BODY, "textbook_context": "x" * 20_001}
    assert client.post("/api/v1/homework-insight", json=body, headers=as_user("alice")).status_code == 422


# --- idle expiry -------------------------------------------------------------


def test_idle_session_expires():
    store = SessionStore()
    s = store.create_session(SessionConfig(), owner_uid="alice")
    s.last_active = datetime.now() - store.IDLE_TTL - timedelta(seconds=1)
    assert store.get_session(s.session_id) is None
    assert s.session_id not in store._sessions


def test_active_session_survives_and_create_prunes_stale():
    store = SessionStore()
    fresh = store.create_session(SessionConfig())
    stale = store.create_session(SessionConfig())
    stale.last_active = datetime.now() - timedelta(hours=4)
    store.create_session(SessionConfig())
    assert stale.session_id not in store._sessions
    assert store.get_session(fresh.session_id) is fresh


# --- playground never in production -----------------------------------------


def test_playground_flag_ignored_in_production(client, monkeypatch):
    monkeypatch.setenv("PLAYGROUND_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert client.get("/api/v1/colearner/playground").status_code == 404
    assert client.post("/api/v1/colearner/cascaded", json={}).status_code == 401
