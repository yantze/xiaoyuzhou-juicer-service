"""Xiaoyuzhou's currently observed private protocol. No embedded account credentials.

QR protocol: official accounts.xiaoyuzhoufm.com JS, inspected 2026-09-21.
Transcript protocol: hesorchen/xiaoyuzhou-juicer (MIT).
"""
import base64
import gzip
import json
import os
import re
import time
from html import unescape
from urllib.parse import urlparse

import httpx

WEB = "https://web-api.xiaoyuzhoufm.com"
API = "https://api.xiaoyuzhoufm.com"
UA = "Xiaoyuzhou/2.7.0 (build:1234; iOS 17.0.0)"
CLIENT = os.getenv("XYZ_CLIENT_ID", "xyz-web")
MIDWAY = os.getenv("XYZ_MIDWAY_APP_ID", "v6worU4NnWyL")  # Public application ID, not a secret.


class ProviderError(Exception):
    def __init__(self, code, message, status=502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def endpoint_error(r):
    if r.status_code in (401, 403):
        raise ProviderError("LOGIN_REQUIRED", "小宇宙登录已失效或当前账号无访问权限，请重新扫码。", 401)
    if r.status_code == 429:
        raise ProviderError("UPSTREAM_RATE_LIMIT", "小宇宙暂时限制请求，请稍后再试。", 429)
    if r.status_code >= 400:
        raise ProviderError("UPSTREAM_ERROR", f"小宇宙接口暂不可用（HTTP {r.status_code}）。")


def json_body(r):
    try:
        b = r.content
        if b.startswith(b"\x1f\x8b"):
            b = gzip.decompress(b)
        return json.loads(b)
    except (ValueError, OSError):
        raise ProviderError("UPSTREAM_FORMAT", "小宇宙返回了无法识别的数据，请稍后再试。") from None


def tokens_from(response, client, old=None):
    old = old or {}
    cookies = {c.name: c.value for c in client.cookies.jar}
    out = {}
    for short in ("access", "refresh"):
        name = f"x-jike-{short}-token"
        value = response.headers.get(name) or cookies.get(name) or old.get(short)
        if value:
            out[short] = value
    if out.get("access") != old.get("access"):
        # A conservative cache lifetime; JWT claims are not treated as authentication.
        exp = time.time() + 3600
        try:
            payload = out["access"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            exp = min(exp, float(claims.get("exp", exp)) - 120)
        except (KeyError, ValueError, IndexError, TypeError):
            pass
        out["expires"] = exp
    else:
        out["expires"] = old.get("expires", 0)
    return out


def episode_id(value):
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{24}", value):
        return value.lower()
    # Accept a pasted share message, but reconstruct all outgoing URLs ourselves.
    match = re.search(r"https://(?:www\.)?xiaoyuzhoufm\.com/episode/([0-9a-fA-F]{24})(?:[/?#\s]|$)", value)
    if not match:
        raise ProviderError("INVALID_EPISODE", "请粘贴有效的小宇宙单集链接。", 400)
    return match[1].lower()


def timestamp(ms):
    total = max(0, int(ms)) // 1000
    return f"{total//3600:02d}:{total//60%60:02d}:{total%60:02d}"


def parse_metadata(html, eid):
    match = re.search(r'<script[^>]*\bid=[\"\']__NEXT_DATA__[\"\'][^>]*>(.*?)</script>', html, re.S)
    try:
        ep = json.loads(match[1])["props"]["pageProps"]["episode"]
        if ep.get("eid") and ep["eid"] != eid:
            raise ValueError()
        title = ep["title"]
    except (TypeError, KeyError, ValueError):
        raise ProviderError("EPISODE_NOT_FOUND", "未找到可读取的单集，或小宇宙页面格式已变更。", 404) from None
    transcript = ep.get("transcript") or {}
    notes = re.sub(r"<(?:br|/p|/div|/li)\b[^>]*>", "\n", ep.get("shownotes") or "", flags=re.I)
    notes = unescape(re.sub(r"<[^>]*>", "", notes))
    return {
        "eid": eid, "title": title, "podcast": (ep.get("podcast") or {}).get("title", ""),
        "duration": ep.get("duration"), "url": f"https://www.xiaoyuzhoufm.com/episode/{eid}",
        "media_id": ep.get("transcriptMediaId") or (transcript.get("mediaId") if isinstance(transcript, dict) else None),
        "shownotes": notes.strip(),
    }


def normalize_segments(data):
    if not isinstance(data, list):
        raise ProviderError("TRANSCRIPT_FORMAT", "逐字稿数据格式已变更。")
    segments = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            continue
        ms = item.get("startMs")
        if not isinstance(ms, (float, int)) or isinstance(ms, bool) or ms < 0 or ms > 864000000:
            raise ProviderError("TRANSCRIPT_FORMAT", "逐字稿时间戳格式已变更。")
        segments.append({"text": item["text"].strip(), "startMs": int(ms)})
    if not segments:
        raise ProviderError("NO_TRANSCRIPT", "这期单集暂时没有可用的逐字稿。", 404)
    return segments


def allowed_cdn(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and any(host == suffix or host.endswith("." + suffix)
            for suffix in ("xyzcdn.net", "xiaoyuzhoufm.com")))


class Xiaoyuzhou:
    def __init__(self, transport=None):
        self.transport = transport

    def client(self, cookies=None):
        return httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            transport=self.transport, cookies=cookies,
            headers={"User-Agent": "Mozilla/5.0", "Origin": "https://accounts.xiaoyuzhoufm.com",
                     "x-jike-allow-app-token-in-cookie": "true"})

    async def create_qr(self):
        async with self.client() as c:
            r = await c.post(WEB + "/v1/auth/qrcode/create", json={"clientId": CLIENT}, headers={"x-midway-app-id": MIDWAY})
            endpoint_error(r)
            data = json_body(r)
            if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not isinstance(data.get("url"), str):
                raise ProviderError("QR_FORMAT", "扫码登录接口已变更，暂时无法创建二维码。")
            return {"id": data["id"], "url": data["url"], "cookies": {x.name: x.value for x in c.cookies.jar},
                    "expires": time.time() + 120, "status": "WAITTING", "last_poll": 0}

    async def poll_qr(self, qr):
        async with self.client(qr.get("cookies")) as c:
            r = await c.post(WEB + "/v1/auth/qrcode/login", json={"id": qr["id"]})
            if r.status_code == 401 and json_body(r).get("code") == 21:
                qr["status"] = "EXPIRED"
                return qr, None, None
            endpoint_error(r)
            data = json_body(r)
            status = data.get("status")
            if status not in ("WAITTING", "SCANNED", "CONFIRMED", "USED"):
                raise ProviderError("QR_FORMAT", "小宇宙返回了未知的扫码状态。")
            qr.update(status=status, cookies={x.name: x.value for x in c.cookies.jar})
            if status in ("CONFIRMED", "USED"):
                tokens = tokens_from(r, c)
                if not tokens.get("access"):
                    raise ProviderError("AUTH_INCOMPATIBLE", "扫码已确认，但平台没有返回可读取文稿的 Token；尚未完成连接。")
                r = await c.get(WEB + "/web/user/get-me", headers={"x-jike-access-token": tokens["access"]})
                endpoint_error(r)
                user = json_body(r).get("data") or {}
                if not user.get("uid"):
                    raise ProviderError("AUTH_INCOMPATIBLE", "未能验证小宇宙登录状态，请重新连接。")
                return qr, tokens, {"nickname": user.get("nickname") or "小宇宙用户"}
            return qr, None, None

    async def refresh(self, tokens, device):
        if not tokens.get("refresh"):
            raise ProviderError("LOGIN_REQUIRED", "登录已过期，请重新扫码。", 401)
        async with self.client() as c:
            r = await c.post(API + "/app_auth_tokens.refresh", content=b"", headers={
                "x-jike-refresh-token": tokens["refresh"], "x-jike-device-id": device,
                "x-jike-app-version": "2.7.0", "User-Agent": UA})
            endpoint_error(r)
            new = tokens_from(r, c, tokens)
            if not r.headers.get("x-jike-access-token") and not any(x.name == "x-jike-access-token" for x in c.cookies.jar):
                raise ProviderError("LOGIN_REQUIRED", "未能续期小宇宙登录，请重新扫码。", 401)
            return new

    async def metadata(self, eid):
        async with self.client() as c:
            r = await c.get(f"https://www.xiaoyuzhoufm.com/episode/{eid}")
            endpoint_error(r)
            return parse_metadata(r.text, eid)

    async def transcript(self, meta, tokens, device):
        if not meta.get("media_id"):
            raise ProviderError("NO_TRANSCRIPT", "这期单集没有提供平台逐字稿。节目介绍不会被当作逐字稿返回。", 404)
        async with self.client() as c:
            r = await c.post(API + "/v1/episode-transcript/get", json={"eid": meta["eid"], "mediaId": meta["media_id"]},
                headers={"x-jike-access-token": tokens["access"], "x-jike-device-id": device,
                         "x-jike-app-version": "2.7.0", "User-Agent": UA})
            endpoint_error(r)
            url = (json_body(r).get("data") or {}).get("transcriptUrl")
            if not isinstance(url, str):
                raise ProviderError("NO_TRANSCRIPT", "当前账号未能获取该单集的逐字稿。", 404)
        # Never send account tokens or cookies to the CDN.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as c:
            for _ in range(4):
                if not allowed_cdn(url):
                    raise ProviderError("CDN_CHANGED", "文稿下载地址不在已验证的平台域名中，请更新服务。")
                async with c.stream("GET", url, headers={"User-Agent": UA}) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get("location", "")))
                        continue
                    endpoint_error(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 20 * 1024 * 1024:
                            raise ProviderError("TOO_LARGE", "文稿超出当前服务的处理大小限制。", 413)
                    try:
                        if raw.startswith(b"\x1f\x8b"):
                            # Some CDNs return gzip bytes without a Content-Encoding header.
                            import io
                            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                                raw = f.read(20 * 1024 * 1024 + 1)
                            if len(raw) > 20 * 1024 * 1024:
                                raise ValueError()
                        return normalize_segments(json.loads(raw))
                    except (ValueError, OSError):
                        raise ProviderError("TRANSCRIPT_FORMAT", "无法解析平台逐字稿。") from None
            raise ProviderError("CDN_CHANGED", "文稿下载重定向次数过多。")
