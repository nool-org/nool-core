"""Live WebSocket channels for curriculum sync - see src/services/realtime.py
for the underlying in-process fan-out. Token-in-query-param auth mirrors
ai_assessor.py's stream_session (browsers/WS clients can't set a header on
the upgrade request).
"""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from shared.errors import UnauthorizedError

from src.api.deps import get_current_user
from src.domain.models import Role
from src.repositories import get_by_firebase_uid, get_session
from src.services.realtime import curriculum_updates, drop_sync_progress_channel, sync_progress_channel

router = APIRouter(prefix="/api/v1", tags=["realtime"])


@router.websocket("/ws/curriculum")
async def curriculum_updates_stream(websocket: WebSocket, token: str = Query(...)) -> None:
    """Any authenticated app user subscribes. Broadcasts a small
    {"type": "curriculum.updated", "subjectId", "board", "grade"} event
    whenever a Super Admin syncs chapters/topics from the KG - see
    admin_catalog.sync_curriculum_from_kg. Already-open teacher/student
    screens (journey map, practice bank, question-paper/voice-test
    builders) listen for this to refetch instead of requiring a manual
    reload - see nool-apps' useCurriculumUpdates hook.
    """
    try:
        auth_user = get_current_user(authorization=f"Bearer {token}")
    except UnauthorizedError:
        await websocket.close(code=4401)
        return

    async with get_session() as db:
        app_user = await get_by_firebase_uid(db, auth_user.uid)
    if app_user is None:
        await websocket.close(code=4401)
        return

    await curriculum_updates.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        curriculum_updates.disconnect(websocket)


@router.websocket("/ws/admin/curriculum-sync/{job_id}")
async def curriculum_sync_progress_stream(websocket: WebSocket, job_id: str, token: str = Query(...)) -> None:
    """Super-Admin-only: live per-chapter/topic progress for one sync run.
    The Super Admin tree-view sync page opens this (with a client-generated
    job_id) before POSTing that same job_id to
    POST /admin/curriculum/sync-from-kg.
    """
    try:
        auth_user = get_current_user(authorization=f"Bearer {token}")
    except UnauthorizedError:
        await websocket.close(code=4401)
        return

    async with get_session() as db:
        app_user = await get_by_firebase_uid(db, auth_user.uid)
    if app_user is None or app_user.role != Role.SUPER_ADMIN:
        await websocket.close(code=4403)
        return

    channel = sync_progress_channel(job_id)
    await channel.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        channel.disconnect(websocket)
        drop_sync_progress_channel(job_id)
