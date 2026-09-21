import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.provider import ProviderError, Xiaoyuzhou, allowed_cdn, episode_id, normalize_segments, parse_metadata
from app.storage import Store

EID = "6aa127229d3264778166855e"


class FakeProvider:
    def __init__(self):
        self.refresh_count = 0
        self.fail_transcript = False

    async def create_qr(self):
        return {"id": "test-only-id", "url": "https://example.com/test-only", "cookies": {}, "expires": time.time() + 120, "status": "WAITTING", "last_poll": 0}

    async def poll_qr(self, qr):
        qr["status"] = "CONFIRMED"
        return qr, {"access": "test-only-access", "refresh": "test-only-refresh", "expires": 0}, {"nickname": "测试用户"}

    async def refresh(self, tokens, device):
        self.refresh_count += 1
        return {"access": "rotated-access", "refresh": "rotated-refresh", "expires": time.time()+3600}

    async def metadata(self, eid):
        return {"eid": eid, "title": "测试播客", "podcast": "测试", "url": f"https://www.xiaoyuzhoufm.com/episode/{eid}", "media_id": "media", "duration": 123}

    async def transcript(self, meta, tokens, device):
        if self.fail_transcript:
            raise ProviderError("UPSTREAM_ERROR", "temporary failure")
        return [{"startMs": 1000, "text": "<script>不会被执行</script>你好"}]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    store = Store(str(tmp_path))
    provider = FakeProvider()
    app = create_app(store, provider)
    with TestClient(app) as client:
        yield app, client, store, provider


def login(client):
    d = client.get("/api/session").json()
    h = {"x-csrf-token": d["csrf"]}
    assert client.post("/api/auth/qr", json={}, headers=h).status_code == 200
    assert client.get("/api/auth/qr/image").headers["content-type"] == "image/png"
    assert client.post("/api/auth/qr/poll", json={}, headers=h).json()["status"] == "CONFIRMED"
    return h


def test_login_transcript_download_and_isolation(setup):
    app, client, store, provider = setup
    headers = login(client)
    status = client.get("/api/session").json()
    assert status["connected"] is True
    assert "test-only" not in json.dumps(status)
    result = client.post("/api/transcripts", json={"url": EID}, headers=headers)
    assert result.status_code == 200
    assert result.json()["segments"][0]["startMs"] == 1000
    assert provider.refresh_count == 1
    for fmt in ("md", "txt", "json"):
        response = client.get("/api/transcripts/download/" + fmt)
        assert response.status_code == 200 and "你好" in response.text
    with TestClient(app) as other:
        other.get("/api/session")
        assert other.get("/api/transcripts/latest").status_code == 404
        assert other.get("/api/session").json()["connected"] is False
    sid = client.cookies.get("xyz_session")
    raw = store.path.read_bytes()
    assert b"test-only-access" not in raw and b"rotated-refresh" not in raw
    client.post("/api/auth/logout", json={}, headers=headers)
    assert store.load(sid) is None
    assert client.get("/api/transcripts/latest").status_code == 401


def test_csrf_and_unauthenticated_access(setup):
    _, client, _, _ = setup
    assert client.get("/api/transcripts/latest").status_code == 401
    d = client.get("/api/session").json()
    assert client.post("/api/auth/qr", json={}).status_code == 403
    assert client.post("/api/auth/qr", json={}, headers={"x-csrf-token": d["csrf"], "origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/transcripts", json={"url":EID}, headers={"x-csrf-token":d["csrf"]}).status_code == 401


def test_rotated_token_saved_even_when_download_fails(setup):
    _, client, store, provider = setup
    h = login(client)
    provider.fail_transcript = True
    assert client.post("/api/transcripts", json={"url":EID}, headers=h).status_code == 502
    assert store.load(client.cookies.get("xyz_session"))["tokens"]["refresh"] == "rotated-refresh"


def test_expired_qr_and_store_restart(setup):
    _, client, store, _ = setup
    d = client.get("/api/session").json()
    h = {"x-csrf-token": d["csrf"]}
    client.post("/api/auth/qr", json={}, headers=h)
    sid = client.cookies.get("xyz_session")
    data = store.load(sid)
    data["qr"]["expires"] = 0
    store.save(sid, data)
    assert client.post("/api/auth/qr/poll", json={}, headers=h).json()["status"] == "EXPIRED"
    assert Store(str(store.path.parent)).load(sid)["csrf"] == d["csrf"]


def test_protocol_cookie_capture_and_unconfirmed_login():
    def handler(r):
        if r.url.path.endswith("/login"):
            return httpx.Response(200, json={"status":"CONFIRMED"}, headers=[("set-cookie", "x-jike-access-token=test-access; Domain=.xiaoyuzhoufm.com; Path=/; HttpOnly"),("set-cookie", "x-jike-refresh-token=test-refresh; Domain=.xiaoyuzhoufm.com; Path=/; HttpOnly")])
        assert r.headers["x-jike-access-token"] == "test-access"
        return httpx.Response(200, json={"data":{"uid":"test-uid","nickname":"测试"}})
    qr, tokens, user = asyncio.run(Xiaoyuzhou(httpx.MockTransport(handler)).poll_qr({"id":"fixture","cookies":{}}))
    assert tokens["access"] == "test-access" and tokens["refresh"] == "test-refresh"
    assert user == {"nickname":"测试"}
    def empty(r):
        return httpx.Response(200, json={"status":"CONFIRMED"})
    with pytest.raises(ProviderError, match="没有返回"):
        asyncio.run(Xiaoyuzhou(httpx.MockTransport(empty)).poll_qr({"id":"fixture","cookies":{}}))


def test_cdn_never_receives_account_tokens():
    calls = []
    def handler(r):
        calls.append(str(r.url))
        if r.url.host == "api.xiaoyuzhoufm.com":
            assert r.headers["x-jike-access-token"] == "test-access"
            return httpx.Response(200, json={"data":{"transcriptUrl":"https://media.xyzcdn.net/test.json"}})
        assert "x-jike-access-token" not in r.headers and "cookie" not in r.headers
        return httpx.Response(200, json=[{"text":"你好","startMs":0}])
    segments = asyncio.run(Xiaoyuzhou(httpx.MockTransport(handler)).transcript({"eid":EID,"media_id":"m"},{"access":"test-access"},"device"))
    assert segments[0]["text"] == "你好" and len(calls) == 2


def test_input_metadata_and_cdn_validation():
    assert episode_id(f"分享：https://www.xiaoyuzhoufm.com/episode/{EID}?x=1") == EID
    for invalid in ("http://127.0.0.1/episode/"+EID, "https://evil.example/episode/"+EID, "abc", EID+"0"):
        with pytest.raises(ProviderError): episode_id(invalid)
    assert allowed_cdn("https://media.xyzcdn.net/a.json")
    for invalid in ("http://media.xyzcdn.net/a", "https://xyzcdn.net.evil.example/a", "https://127.0.0.1/a", "https://user@media.xyzcdn.net/a"):
        assert not allowed_cdn(invalid)
    html = '<script id="__NEXT_DATA__">'+json.dumps({"props":{"pageProps":{"episode":{"eid":EID,"title":"节目","transcript":{"mediaId":"m"}}}}})+'</script>'
    assert parse_metadata(html,EID)["media_id"] == "m"
    with pytest.raises(ProviderError): normalize_segments([{"text":"a","startMs":"oops"}])
