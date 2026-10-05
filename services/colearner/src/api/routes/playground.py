from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from src.api.deps import playground_active
from src.config.settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["colearner-playground"])

_PAGE = Path(__file__).resolve().parents[2] / "playground" / "index.html"


@router.get("/colearner/playground", include_in_schema=False)
def playground() -> FileResponse:
    """Browser test page for all three pipelines - see src/playground/.
    404s like any unknown route unless PLAYGROUND_ENABLED is on (never in production)."""
    if not playground_active(get_settings()):
        raise HTTPException(status_code=404)
    return FileResponse(_PAGE, media_type="text/html")
