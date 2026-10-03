from src.config.settings import get_settings


def asset_url(blob: str) -> str | None:
    """Public URL for a question-assets blob path - see CoreSettings.asset_base_url."""
    base = get_settings().asset_base_url
    return f"{base.rstrip('/')}/{blob}" if base else None
