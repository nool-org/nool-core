import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from src.domain.schemas import SessionConfig


class SessionState:
    def __init__(self, session_id: str, config: SessionConfig, owner_uid: Optional[str] = None):
        self.session_id: str = session_id
        self.config: SessionConfig = config
        # Firebase uid of the creator; None for sessions not bound to a
        # caller (realtime_service tracks ownership itself via owner_ref).
        self.owner_uid: Optional[str] = owner_uid
        self.created_at: datetime = datetime.now()
        self.last_active: datetime = self.created_at
        self.turn_index: int = 0
        self.is_completed: bool = False
        self.turns: List[Dict[str, str]] = []

        # Chat history for the cascaded prototype (see cascaded_service.py).
        self.openai_messages: List[Dict[str, Any]] = []
        self.report: Optional[Dict[str, Any]] = None

        # Token and cost metrics
        self.turn_metrics: List[Dict[str, Any]] = []
        self.cost_report: Optional[Dict[str, Any]] = None
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_audio_seconds: float = 0.0
        self.total_cost_usd: float = 0.0


class SessionStore:
    """In-memory only - v1 limitation: sessions do not survive a container
    restart and are not shared across replicas. Acceptable for now (single
    worker, no --reload multi-process in a real deploy, matching how
    auth/core/kg already run) rather than adding Redis/a DB for this alone
    (see nool-core/claude.md: don't add infra without an actual
    requirement). Revisit if this service needs to scale horizontally or
    survive restarts mid-session.
    """

    IDLE_TTL = timedelta(hours=3)

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionState] = {}

    def _expired(self, session: SessionState) -> bool:
        return datetime.now() - session.last_active > self.IDLE_TTL

    def _prune(self) -> None:
        # ponytail: O(n) sweep per create, fine for in-memory v1 sizes.
        for sid, session in list(self._sessions.items()):
            if self._expired(session):
                del self._sessions[sid]

    def create_session(self, config: SessionConfig, owner_uid: Optional[str] = None) -> SessionState:
        self._prune()
        session_id = str(uuid.uuid4())
        session = SessionState(session_id=session_id, config=config, owner_uid=owner_uid)
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str, owner_uid: Optional[str] = None) -> Optional[SessionState]:
        """None when unknown, idle-expired, or (if owner_uid is given) owned
        by someone else - callers 404 on all three alike, so a session_id
        alone never reveals that someone else's session exists."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if self._expired(session):
            del self._sessions[session_id]
            return None
        if owner_uid is not None and session.owner_uid != owner_uid:
            return None
        session.last_active = datetime.now()
        return session

    def save_session(self, session: SessionState) -> None:
        session.last_active = datetime.now()
        self._sessions[session.session_id] = session

    def delete_session(self, session_id: str) -> bool:
        self._sessions.pop(session_id, None)
        return True


session_store = SessionStore()
