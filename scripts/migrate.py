"""Initialize the application's private Supabase schema; never log credentials."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from app.postgres import PostgresStore, StorageError
from app.store_backend import database_url


async def main():
    dsn = os.getenv("MIGRATION_DATABASE_URL") or database_url()
    if not dsn:
        raise ValueError("Set MIGRATION_DATABASE_URL or DATABASE_URL/POSTGRES_URL")
    store = PostgresStore(dsn, os.getenv("TOKEN_ENCRYPTION_KEY", ""))
    async with await psycopg.AsyncConnection.connect(**store.connection_options) as conn:
        # The file owns a single BEGIN/COMMIT transaction, safe for poolers.
        await conn.execute((Path(__file__).resolve().parents[1] / "migrations/001_supabase.sql").read_text())
    await store.health()
    print("Supabase schema initialized and verified.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (psycopg.Error, StorageError):
        sys.exit("Database migration failed. Check connection access and backend role permissions.")
    except ValueError as error:
        sys.exit(str(error))
