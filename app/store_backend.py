"""Select durable PostgreSQL in production; retain SQLite for local/Railway."""
import asyncio
import hashlib
import os
import time
from collections import defaultdict, deque

from .postgres import PostgresStore
from .provider import ProviderError
from .storage import Store


def database_url():
    return next((os.environ[k] for k in ("DATABASE_URL", "POSTGRES_URL", "SUPABASE_DB_URL") if os.getenv(k)), None)


class SQLiteBackend:
    def __init__(self, store):
        self.store = store
        self.locks = [asyncio.Lock() for _ in range(128)]
        self.limits = defaultdict(deque)

    async def create(self):
        return await asyncio.to_thread(self.store.create)

    async def load(self, sid):
        return await asyncio.to_thread(self.store.load, sid)

    async def save(self, sid, data):
        await asyncio.to_thread(self.store.save, sid, data)

    async def delete(self, sid):
        await asyncio.to_thread(self.store.delete, sid)

    async def health(self):
        def check():
            with self.store.connect() as conn:
                conn.execute("SELECT 1 FROM sessions LIMIT 0")
        await asyncio.to_thread(check)

    def lock(self, sid):
        return self.locks[int(hashlib.sha256(sid.encode()).hexdigest()[:8], 16) % len(self.locks)]

    async def limit(self, key, count, window):
        now = time.monotonic()
        if len(self.limits) > 10000:
            for k in list(self.limits):
                if not self.limits[k] or self.limits[k][-1] < now - 3600:
                    del self.limits[k]
            if len(self.limits) > 10000:
                raise ProviderError("RATE_LIMIT", "服务繁忙，请稍后再试。", 429)
        q = self.limits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= count:
            raise ProviderError("RATE_LIMIT", "操作过于频繁，请稍后再试。", 429)
        q.append(now)


def create_backend(store=None):
    if store is not None:
        return SQLiteBackend(store) if isinstance(store, Store) else store
    dsn = database_url()
    if dsn:
        return PostgresStore(dsn, os.getenv("TOKEN_ENCRYPTION_KEY", ""))
    if os.getenv("VERCEL"):
        raise ValueError("Vercel requires DATABASE_URL or POSTGRES_URL from the connected Supabase database")
    return SQLiteBackend(Store(os.getenv("DATA_DIR", "./data"), os.getenv("TOKEN_ENCRYPTION_KEY")))
