"""Configure the shared PostgreSQL store used by Vercel and local development."""
import os

from .postgres import PostgresStore


def database_url():
    return next((os.environ[k] for k in ("DATABASE_URL", "POSTGRES_URL", "SUPABASE_DB_URL") if os.getenv(k)), None)


def create_backend(store=None):
    if store is not None:
        return store
    dsn = database_url()
    if not dsn:
        raise ValueError("Vercel requires DATABASE_URL or POSTGRES_URL from the connected Supabase database")
    return PostgresStore(dsn, os.getenv("TOKEN_ENCRYPTION_KEY", ""))
