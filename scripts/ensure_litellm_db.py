"""Creates the AI gateway's own database (`litellm`, compose service litellm)
if it doesn't exist yet - run by the one-shot migrate service before Alembic,
since Postgres only runs init scripts on a brand-new volume."""
import asyncio
import os

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
                                 database=os.environ["POSTGRES_DB"], host=os.environ.get("POSTGRES_HOST", "postgres"),
                                 port=int(os.environ.get("POSTGRES_PORT", "5432")))
    try:
        if not await conn.fetchval("select 1 from pg_database where datname = 'litellm'"):
            await conn.execute("create database litellm")
            print("created database litellm")
    finally:
        await conn.close()


asyncio.run(main())
