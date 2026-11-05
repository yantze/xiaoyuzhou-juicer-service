"""Test-only encrypted disk fixture; never imported by the application."""
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from cryptography.fernet import Fernet

from app.provider import ProviderError


class Store:
    def __init__(self, directory: str, key: str | None = None):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        if not key:
            keyfile = root / "encryption.key"
            if not keyfile.exists():
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            key = keyfile.read_text().strip()
        self.cipher = Fernet(key.encode())
        self.path = root / "sessions.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data BLOB NOT NULL, expires REAL NOT NULL)")
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def digest(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def create(self):
        sid = secrets.token_urlsafe(32)
        data = {"csrf": secrets.token_urlsafe(32), "device": secrets.token_hex(16), "created": time.time()}
        self.save(sid, data)
        return sid, data

    def load(self, sid):
        if not sid or len(sid) != 43:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM sessions WHERE id=? AND expires>?", (self.digest(sid), time.time())).fetchone()
        if row is None:
            return None
        return json.loads(self.cipher.decrypt(row[0]))

    def save(self, sid, data):
        raw = self.cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (self.digest(sid), raw, time.time() + 30 * 86400))
            conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))

    def delete(self, sid):
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id=?", (self.digest(sid),))



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
