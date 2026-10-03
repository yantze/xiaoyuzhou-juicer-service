"""Integration tests against a disposable PostgreSQL, never a user's database.

Run with RUN_POSTGRES_TESTS=1 after installing requirements-dev.txt.
"""
import asyncio
import json
import os
from pathlib import Path

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet

from app.main import create_app
from app.postgres import PostgresStore, StorageError
from app.provider import ProviderError
from app.store_backend import create_backend
from test_service import EID, FakeProvider


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    if os.getenv("RUN_POSTGRES_TESTS") != "1":
        pytest.skip("Requires explicit RUN_POSTGRES_TESTS=1; creates a disposable local server")
    dsn = os.getenv("LOCAL_POSTGRES_TEST_URL")
    server = None
    if dsn:
        # An explicitly supplied disposable local test server (e.g. CI service).
        assert psycopg.conninfo.conninfo_to_dict(dsn).get("host") in ("127.0.0.1", "localhost", "::1")
    else:
        pgserver = pytest.importorskip("pgserver")
        root = tmp_path_factory.mktemp("xyz-postgres")
        server = pgserver.get_server(root / "db", cleanup_mode="delete")
        dsn = server.get_uri()
    # These roles exercise Supabase's browser permission boundary.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE ROLE anon; CREATE ROLE authenticated")
        conn.execute("CREATE TABLE public.keep_existing (value text)")
        conn.execute("INSERT INTO public.keep_existing VALUES ('preserve me')")
        migration = (Path(__file__).resolve().parents[1] / "migrations/001_supabase.sql").read_text()
        conn.execute(migration)
        conn.execute(migration)
        assert conn.execute("SELECT value FROM public.keep_existing").fetchone()[0] == "preserve me"
    yield dsn, Fernet.generate_key().decode()
    if server is not None:
        server.cleanup()


def make_store(postgres):
    return PostgresStore(*postgres)


def test_backend_requires_durable_configuration(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "POSTGRES_URL", "SUPABASE_DB_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "must-not-exist"))
    with pytest.raises(ValueError, match="Vercel requires"):
        create_backend()
    assert not (tmp_path / "must-not-exist").exists()
    monkeypatch.setenv("POSTGRES_URL", "postgresql://fixture:fixture@localhost/db")
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        create_backend()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    store = create_backend()
    assert isinstance(store, PostgresStore)
    assert store.connection_options["prepare_threshold"] is None
    assert store.connection_options["sslmode"] == "require"


def test_postgres_storage_encryption_expiry_and_role_isolation(postgres):
    async def run():
        store = make_store(postgres)
        await store.health()
        sid, data = await store.create()
        data["tokens"] = {"access": "test-only-access-secret", "refresh": "test-only-refresh-secret"}
        async with store.lock(sid):
            await store.save(sid, data)
        other = make_store(postgres)
        assert (await other.load(sid))["tokens"] == data["tokens"]
        row = await store.query("SELECT id,data FROM xyz_juicer.sessions WHERE id=%s", (store.digest(sid),), fetch=True)
        assert row[0] != sid and b"test-only-access-secret" not in bytes(row[1])
        with psycopg.connect(postgres[0], autocommit=True) as conn:
            for role in ("anon", "authenticated"):
                conn.execute(f"SET ROLE {role}")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("SELECT * FROM xyz_juicer.sessions")
                conn.execute("RESET ROLE")
        await store.query("UPDATE xyz_juicer.sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (store.digest(sid),))
        assert await other.load(sid) is None
        await other.create()  # Prune a bounded number of expired rows.
        assert await store.query("SELECT id FROM xyz_juicer.sessions WHERE id=%s", (store.digest(sid),), fetch=True) is None
    asyncio.run(run())


def test_postgres_rate_limit_shared_between_instances(postgres):
    async def run():
        stores = [make_store(postgres), make_store(postgres)]
        key = ("integration-rate", os.urandom(12).hex())
        async def attempt(index):
            try:
                await stores[index % 2].limit(key, 3, 3600)
                return True
            except ProviderError as error:
                assert error.code == "RATE_LIMIT" and error.status == 429
                return False
        outcomes = await asyncio.gather(*(attempt(i) for i in range(10)))
        assert sum(outcomes) == 3
    asyncio.run(run())


def test_expired_owner_cannot_overwrite_or_release_new_lease(postgres):
    async def run():
        old, new = make_store(postgres), make_store(postgres)
        sid, data = await old.create()
        old_lock = old.lock(sid)
        await old_lock.__aenter__()
        await old.query("UPDATE xyz_juicer.leases SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=%s", (old.digest(sid),))
        async with new.lock(sid):
            data["tokens"] = {"refresh": "new-owner-refresh"}
            await new.save(sid, data)
            with pytest.raises(ProviderError, match="超时"):
                await old.save(sid, {"tokens": {"refresh": "stale-refresh"}})
            with pytest.raises(ProviderError):
                await old.delete(sid)
            await old_lock.__aexit__(None, None, None)
            # The old finally block must not delete the new owner's lock.
            await new.save(sid, data)
            assert (await new.load(sid))["tokens"]["refresh"] == "new-owner-refresh"
            await new.delete(sid)
            with pytest.raises(ProviderError):
                await new.save(sid, data)  # Never resurrect a logged-out session.
        assert await old.load(sid) is None
    asyncio.run(run())


def test_cross_instance_api_login_refresh_failure_download_and_logout(postgres):
    async def run():
        provider = FakeProvider()
        stores = [make_store(postgres), make_store(postgres)]
        apps = [create_app(s, provider) for s in stores]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(apps[0]), base_url="https://fixture.test") as first:
            data = (await first.get("/api/session")).json()
            headers = {"x-csrf-token": data["csrf"], "origin": "https://fixture.test"}
            assert (await first.post("/api/auth/qr", json={}, headers=headers)).status_code == 200
            async with httpx.AsyncClient(transport=httpx.ASGITransport(apps[1]), base_url="https://fixture.test", cookies=first.cookies) as second:
                assert (await second.get("/api/auth/qr/image")).headers["content-type"] == "image/png"
                assert (await second.post("/api/auth/qr/poll", json={}, headers=headers)).json()["status"] == "CONFIRMED"
                provider.fail_transcript = True
                assert (await second.post("/api/transcripts", json={"url": EID}, headers=headers)).status_code == 502
                assert (await stores[0].load(first.cookies.get("xyz_session")))["tokens"]["refresh"] == "rotated-refresh"
                provider.fail_transcript = False
                assert (await first.post("/api/transcripts", json={"url": EID}, headers=headers)).status_code == 200
                assert provider.refresh_count == 1
                status = (await second.get("/api/session")).json()
                assert status["connected"] and "rotated-refresh" not in json.dumps(status)
                for fmt in ("md", "txt", "json"):
                    assert (await second.get("/api/transcripts/download/" + fmt)).status_code == 200
                assert (await second.post("/api/auth/logout", json={}, headers=headers)).status_code == 200
                assert (await first.get("/api/transcripts/latest")).status_code == 401
    asyncio.run(run())


def test_concurrent_instances_do_not_rotate_tokens_twice(postgres):
    async def run():
        started, proceed = asyncio.Event(), asyncio.Event()
        class SlowProvider(FakeProvider):
            async def refresh(self, tokens, device):
                started.set()
                await proceed.wait()
                return await super().refresh(tokens, device)
        provider = SlowProvider()
        stores = [make_store(postgres), make_store(postgres)]
        sid, data = await stores[0].create()
        data["tokens"] = {"access": "test-only", "refresh": "test-only", "expires": 0}
        async with stores[0].lock(sid):
            await stores[0].save(sid, data)
        apps = [create_app(s, provider) for s in stores]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(apps[0]), base_url="https://fixture.test", cookies={"xyz_session": sid}) as first:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(apps[1]), base_url="https://fixture.test", cookies={"xyz_session": sid}) as second:
                headers = {"x-csrf-token": data["csrf"]}
                pending = asyncio.create_task(first.post("/api/transcripts", json={"url": EID}, headers=headers))
                await asyncio.wait_for(started.wait(), 5)
                try:
                    result = await second.post("/api/transcripts", json={"url": EID}, headers=headers)
                    assert result.status_code == 409 and result.json()["code"] == "SESSION_BUSY"
                    logout = await second.post("/api/auth/logout", json={}, headers=headers)
                    assert logout.status_code == 409
                finally:
                    proceed.set()
                assert (await pending).status_code == 200
                assert provider.refresh_count == 1
    asyncio.run(run())


def test_database_errors_are_sanitized(postgres):
    async def run():
        store = make_store(postgres)
        with pytest.raises(StorageError) as error:
            await store.query("SELECT 'test-only-secret' FROM xyz_juicer.no_such_table")
        assert "test-only-secret" not in str(error.value) and postgres[0] not in str(error.value)
    asyncio.run(run())
