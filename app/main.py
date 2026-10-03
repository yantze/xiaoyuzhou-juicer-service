import asyncio
import io
import json
import os
import secrets
import time
from pathlib import Path

import httpx
import qrcode
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .provider import ProviderError, Xiaoyuzhou, episode_id, timestamp
from .postgres import StorageError
from .store_backend import create_backend

STATIC = Path(__file__).parent / "static"
COOKIE = "xyz_session"


def create_app(store=None, provider=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    db = create_backend(store)
    xyz = provider or Xiaoyuzhou()
    capacity = asyncio.Semaphore(12)
    app.state.store = db

    async def session(request):
        sid = request.cookies.get(COOKIE)
        data = await db.load(sid)
        if data is None:
            raise ProviderError("SESSION_EXPIRED", "页面会话已过期，请刷新页面。", 401)
        if request.method != "GET" and not secrets.compare_digest(request.headers.get("x-csrf-token", ""), data["csrf"]):
            raise ProviderError("CSRF", "页面验证失败，请刷新页面。", 403)
        return sid, data

    @app.middleware("http")
    async def security(request, call_next):
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > 8192:
            return JSONResponse({"error": "请求内容过大"}, status_code=413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "不允许跨站请求"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "不允许跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, e):
        return JSONResponse({"error": e.message, "code": e.code}, status_code=e.status)

    @app.exception_handler(httpx.HTTPError)
    async def network_error(request, e):
        # Never reflect upstream URLs, headers or cookie jars in errors/logs.
        return JSONResponse({"error": "连接小宇宙超时或网络异常，请稍后再试。", "code": "NETWORK_ERROR"}, status_code=502)

    @app.exception_handler(StorageError)
    async def storage_error(request, e):
        return JSONResponse({"error": str(e), "code": "STORAGE_UNAVAILABLE"}, status_code=503)

    @app.exception_handler(TimeoutError)
    async def request_timeout(request, e):
        return JSONResponse({"error": "请求处理超时，请稍后重试。", "code": "REQUEST_TIMEOUT"}, status_code=504)

    @app.get("/health")
    async def health():
        await db.health()
        return {"ok": True}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/session")
    async def status(request: Request):
        sid = request.cookies.get(COOKIE)
        data = await db.load(sid)
        if data is None:
            await db.limit(("new-session", (request.client.host if request.client else "unknown")), 60, 3600)
            sid, data = await db.create()
        result = data.get("result")
        response = JSONResponse({"connected": bool(data.get("tokens")), "user": data.get("user"),
            "csrf": data["csrf"], "lastEpisode": result["meta"]["eid"] if result else None})
        response.set_cookie(COOKIE, sid, httponly=True, secure=os.getenv("COOKIE_SECURE", "true") != "false",
                            samesite="strict", max_age=30 * 86400, path="/")
        return response

    @app.post("/api/auth/qr")
    async def start_qr(request: Request):
        sid, _ = await session(request)
        await db.limit(("qr", (request.client.host if request.client else "unknown")), 20, 3600)
        async with asyncio.timeout(100), capacity, db.lock(sid):
            data = await db.load(sid)
            if not data:
                raise ProviderError("SESSION_EXPIRED", "页面会话已过期，请刷新页面。", 401)
            data["qr"] = await xyz.create_qr()
            await db.save(sid, data)
            return {"status": "WAITTING", "expiresAt": data["qr"]["expires"]}

    @app.get("/api/auth/qr/image")
    async def qr_image(request: Request):
        _, data = await session(request)
        qr = data.get("qr")
        if not qr or qr["expires"] <= time.time():
            raise ProviderError("QR_EXPIRED", "二维码已过期，请重新生成。", 410)
        image = qrcode.make(qr["url"], box_size=8, border=4)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png")

    @app.post("/api/auth/qr/poll")
    async def poll(request: Request):
        sid, _ = await session(request)
        async with asyncio.timeout(100), capacity, db.lock(sid):
            data = await db.load(sid)
            if not data:
                raise ProviderError("SESSION_EXPIRED", "页面会话已过期，请刷新页面。", 401)
            qr = data.get("qr")
            if not qr:
                return {"status": "CONFIRMED" if data.get("tokens") else "EXPIRED"}
            if qr["expires"] <= time.time():
                data.pop("qr", None)
                await db.save(sid, data)
                return {"status": "EXPIRED"}
            if time.time() - qr["last_poll"] < 1.5:
                return {"status": qr["status"]}
            qr["last_poll"] = time.time()
            qr, tokens, user = await xyz.poll_qr(qr)
            if tokens:
                data.update(tokens=tokens, user=user)
                data.pop("qr", None)
            else:
                data["qr"] = qr
            await db.save(sid, data)
            return {"status": qr["status"], "user": user}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        sid, _ = await session(request)
        async with asyncio.timeout(100), db.lock(sid):
            await db.delete(sid)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    class EpisodeInput(BaseModel):
        url: str = Field(min_length=1, max_length=2000)

    @app.post("/api/transcripts")
    async def fetch_transcript(request: Request, body: EpisodeInput):
        eid = episode_id(body.url)
        sid, _ = await session(request)
        await db.limit(("transcript", sid), 20, 3600)
        async with asyncio.timeout(100), capacity, db.lock(sid):
            data = await db.load(sid)
            if not data or not data.get("tokens"):
                raise ProviderError("LOGIN_REQUIRED", "请先连接你的小宇宙账号。", 401)
            if data.get("result", {}).get("meta", {}).get("eid") == eid:
                return data["result"]
            meta = await xyz.metadata(eid)
            if not meta.get("media_id"):
                raise ProviderError("NO_TRANSCRIPT", "这期单集没有提供平台逐字稿。", 404)
            tokens = data["tokens"]
            if tokens.get("expires", 0) <= time.time():
                tokens = data["tokens"] = await xyz.refresh(tokens, data["device"])
                await db.save(sid, data)  # Persist the rotated token before another network request.
            try:
                segments = await xyz.transcript(meta, tokens, data["device"])
            except ProviderError as e:
                if e.status != 401 or not tokens.get("refresh"):
                    raise
                tokens = data["tokens"] = await xyz.refresh(tokens, data["device"])
                await db.save(sid, data)
                segments = await xyz.transcript(meta, tokens, data["device"])
            result = {"meta": {k: v for k, v in meta.items() if k not in ("media_id", "shownotes")},
                      "segments": segments, "source": "xiaoyuzhou-asr", "fetchedAt": time.time()}
            data["result"] = result
            await db.save(sid, data)
            return result

    @app.get("/api/transcripts/latest")
    async def last_transcript(request: Request):
        _, data = await session(request)
        if not data.get("result"):
            raise ProviderError("NOT_FOUND", "还没有获取逐字稿。", 404)
        return data["result"]

    @app.get("/api/transcripts/download/{fmt}")
    async def download(request: Request, fmt: str):
        _, data = await session(request)
        result = data.get("result")
        if not result:
            raise ProviderError("NOT_FOUND", "还没有获取逐字稿。", 404)
        if fmt not in ("md", "txt", "json"):
            raise ProviderError("INVALID_FORMAT", "不支持该文件格式。", 400)
        meta = result["meta"]
        if fmt == "json":
            text = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            text = ("# " if fmt == "md" else "") + meta["title"] + "\n\n"
            text += f"来源：小宇宙自动 ASR；人名与术语可能有误。\n{meta['url']}\n\n"
            text += "\n\n".join(f"[{timestamp(s['startMs'])}] {s['text']}" for s in result["segments"])
        return Response(text, media_type="application/json" if fmt == "json" else "text/plain",
            headers={"Content-Disposition": f'attachment; filename="{meta["eid"]}.{fmt}"'})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
