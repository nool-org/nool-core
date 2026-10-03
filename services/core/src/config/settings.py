from shared.config import BaseServiceSettings


class CoreSettings(BaseServiceSettings):
    service_name: str = "core"

    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    firebase_project_id: str | None = None
    firebase_credentials_path: str | None = None

    # Blank-safe for local dev, same as the Firebase settings above - see
    # src/services/email.py. Only needed for School Admin's "send
    # credentials by email" action; nothing else in this service sends mail.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None

    # Where a School Admin's "Request an upgrade" click gets emailed - see
    # POST /school/subscription/upgrade-request. Blank-safe like the SMTP
    # settings above: with no SMTP configured, or this unset, the request
    # still records to the audit log, it just isn't emailed anywhere.
    sales_email: str | None = None

    # Base URL of the kg service (services/kg) - see src/services/kg_client.py.
    # Blank-safe: only POST /admin/curriculum/sync-from-kg and Question
    # Paper generation need it; every other route is unaffected if unset.
    kg_service_url: str | None = None

    # Base URL of the colearner service (services/colearner) - see
    # src/services/colearner_client.py. Blank-safe: only the AI Assessor's
    # WS stream (ai_assessor.py's stream_session) needs it; every other
    # route is unaffected if unset (the WS closes with an error event
    # instead of connecting to a real voice pipeline).
    colearner_service_url: str | None = None

    # Base URL of the generation service (services/generation) - see
    # src/services/generation_client.py. Blank-safe: homework is then built
    # from the question bank plus the existing KG/deterministic fallback.
    generation_service_url: str | None = None

    # Public base of the question-assets blob container - figure `blob`
    # paths (DatasetQuestion.figures) are resolved against it in responses,
    # so moving storage accounts (Azurite -> Azure) is config-only.
    # Blank-safe: figures come back with url=None.
    asset_base_url: str | None = None

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy connection string.

        Always resolves to the ``postgres`` Compose service hostname inside
        containers - never localhost - per CLAUDE.md.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def get_settings() -> CoreSettings:
    """Reads settings from the environment on every call so tests can vary
    env vars per-case without dealing with cache invalidation.
    """
    return CoreSettings()
