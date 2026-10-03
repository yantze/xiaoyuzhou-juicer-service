"""Encrypted, shared session storage for Vercel and Supabase PostgreSQL.

Connections are short-lived and never held during upstream HTTP requests.
Leases fence writes so a timed-out invocation cannot overwrite a newer login.
"""
import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.conninfo import conninfo_to_dict

from .provider import ProviderError


class StorageError(Exception):
    """Safe to show to the browser; must never contain a DSN or token."""


class PostgresStore:
    # Longer than the Vercel request budget (120s). Expired owners cannot save.
    LEASE_SECONDS = 150

    def __init__(self, dsn: str, key: str):
        if not key:
            raise ValueError("Supabase storage requires TOKEN_ENCRYPTION_KEY")
        try:
            self.cipher = Fernet(key.encode())
            self.connection_options = conninfo_to_dict(dsn)
        except (ValueError, psycopg.Error):
            raise ValueError("Invalid database or encryption configuration") from None
        host = self.connection_options.get("host", "")
        local = host in ("localhost", "127.0.0.1", "::1") or host.startswith("/")
        if os.getenv("VERCEL") or not local:
            # Keep stronger verification settings supplied by the integration.
            if self.connection_options.get("sslmode") not in ("require", "verify-ca", "verify-full"):
                self.connection_options["sslmode"] = "require"
        self.connection_options.update(connect_timeout=10, prepare_threshold=None, autocommit=True)
        self._lease = ContextVar(f"xyz_lease_{id(self)}", default=None)
        self._connections = asyncio.Semaphore(1)

    @staticmethod
    def digest(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    async def query(self, sql, params=(), *, fetch=False):
        try:
            async with self._connections:
                async with await psycopg.AsyncConnection.connect(**self.connection_options) as conn:
                    async with conn.transaction():
                        # Transaction-local settings are safe with Supavisor/PgBouncer.
                        await conn.execute("SET LOCAL statement_timeout = '10s'")
                        cursor = await conn.execute(sql, params)
                        return await cursor.fetchone() if fetch else None
        except psycopg.Error:
            raise StorageError("数据库暂不可用，请稍后重试；首次部署请先完成数据库初始化。") from None

    async def health(self):
        # Verify both connectivity and all application tables, without data.
        await self.query("SELECT 1 FROM xyz_juicer.sessions LIMIT 0")
        await self.query("SELECT 1 FROM xyz_juicer.leases LIMIT 0")
        await self.query("SELECT 1 FROM xyz_juicer.rate_limits LIMIT 0")

    async def create(self):
        sid = secrets.token_urlsafe(32)
        data = {"csrf": secrets.token_urlsafe(32), "device": secrets.token_hex(16), "created": time.time()}
        await self.save(sid, data)
        # Bounded lazy retention cleanup; no cron or extra service is needed.
        await self.query(
            "WITH removed_sessions AS (DELETE FROM xyz_juicer.sessions WHERE id IN "
            "(SELECT id FROM xyz_juicer.sessions WHERE expires_at <= clock_timestamp() LIMIT 100)), "
            "removed_leases AS (DELETE FROM xyz_juicer.leases WHERE session_id IN "
            "(SELECT session_id FROM xyz_juicer.leases WHERE expires_at <= clock_timestamp() LIMIT 100)) "
            "DELETE FROM xyz_juicer.rate_limits WHERE id IN "
            "(SELECT id FROM xyz_juicer.rate_limits WHERE expires_at <= clock_timestamp() LIMIT 100)"
        )
        return sid, data

    async def load(self, sid):
        if not sid or len(sid) != 43:
            return None
        row = await self.query(
            "SELECT data FROM xyz_juicer.sessions WHERE id=%s AND expires_at > clock_timestamp()",
            (self.digest(sid),), fetch=True,
        )
        if row is None:
            return None
        try:
            return json.loads(self.cipher.decrypt(bytes(row[0])))
        except (InvalidToken, ValueError):
            raise StorageError("无法解密已保存的会话，请检查 TOKEN_ENCRYPTION_KEY 是否与部署前一致。") from None

    async def save(self, sid, data):
        raw = self.cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())
        lease = self._lease.get()
        session_id = self.digest(sid)
        if lease is None:
            # Only new sessions and explicit offline imports may write unlocked.
            await self.query(
                "INSERT INTO xyz_juicer.sessions (id,data,expires_at) "
                "VALUES (%s,%s,clock_timestamp()+interval '30 days') "
                "ON CONFLICT (id) DO NOTHING", (session_id, raw),
            )
            return
        if lease[0] != session_id:
            raise StorageError("会话锁不匹配，请刷新页面。")
        row = await self.query(
            "UPDATE xyz_juicer.sessions SET data=%s, expires_at=clock_timestamp()+interval '30 days' "
            "WHERE id=%s AND EXISTS (SELECT 1 FROM xyz_juicer.leases "
            "WHERE session_id=%s AND owner=%s AND expires_at > clock_timestamp() FOR UPDATE) RETURNING id",
            (raw, session_id, *lease), fetch=True,
        )
        if row is None:
            raise ProviderError("SESSION_BUSY", "会话操作已超时或失效，请刷新后重试。", 409)

    async def delete(self, sid):
        lease = self._lease.get()
        if not lease or lease[0] != self.digest(sid):
            raise StorageError("清除会话需要持有会话锁。")
        row = await self.query(
            "DELETE FROM xyz_juicer.sessions WHERE id=%s AND EXISTS "
            "(SELECT 1 FROM xyz_juicer.leases WHERE session_id=%s AND owner=%s "
            "AND expires_at > clock_timestamp() FOR UPDATE) RETURNING id", (lease[0], *lease), fetch=True,
        )
        if row is None:
            raise ProviderError("SESSION_EXPIRED", "页面会话已过期，请刷新页面。", 401)

    @asynccontextmanager
    async def lock(self, sid):
        session_id, owner = self.digest(sid), secrets.token_hex(24)
        row = await self.query(
            "INSERT INTO xyz_juicer.leases (session_id,owner,expires_at) "
            "VALUES (%s,%s,clock_timestamp()+%s*interval '1 second') "
            "ON CONFLICT (session_id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at "
            "WHERE xyz_juicer.leases.expires_at <= clock_timestamp() RETURNING owner",
            (session_id, owner, self.LEASE_SECONDS), fetch=True,
        )
        if row is None:
            raise ProviderError("SESSION_BUSY", "当前会话正在处理中，请稍后重试。", 409)
        context = self._lease.set((session_id, owner))
        try:
            yield
        finally:
            self._lease.reset(context)
            # Guard by owner: an old invocation must never release a newer lease.
            with suppress(StorageError):
                await self.query("DELETE FROM xyz_juicer.leases WHERE session_id=%s AND owner=%s", (session_id, owner))

    async def limit(self, key, count, window):
        digest = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
        row = await self.query(
            "INSERT INTO xyz_juicer.rate_limits (id,count,expires_at) "
            "VALUES (%s,1,clock_timestamp()+%s*interval '1 second') "
            "ON CONFLICT (id) DO UPDATE SET "
            "count=CASE WHEN xyz_juicer.rate_limits.expires_at <= clock_timestamp() "
            "THEN 1 ELSE LEAST(xyz_juicer.rate_limits.count+1,%s+1) END, "
            "expires_at=CASE WHEN xyz_juicer.rate_limits.expires_at <= clock_timestamp() "
            "THEN excluded.expires_at ELSE xyz_juicer.rate_limits.expires_at END RETURNING count",
            (digest, window, count), fetch=True,
        )
        if row[0] > count:
            raise ProviderError("RATE_LIMIT", "操作过于频繁，请稍后再试。", 429)
