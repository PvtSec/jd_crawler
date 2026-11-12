"""Shared async HTTP client: global + per-host concurrency limits, retries with
backoff (honouring Retry-After), conditional requests and body-hash change
detection so unchanged feeds are not re-parsed."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import FetchCfg

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class PageResult:
    status: int
    final_url: str
    text: str


class Http:
    def __init__(self, cfg: FetchCfg):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10),
            headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip, deflate"},
            limits=httpx.Limits(max_connections=cfg.max_concurrency * 2, max_keepalive_connections=cfg.max_concurrency),
            follow_redirects=True,
        )
        self._global = asyncio.Semaphore(cfg.max_concurrency)
        self._hosts: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(cfg.per_host_concurrency))
        # url -> (etag, last-modified, body sha1)
        self._validators: dict[str, tuple[str | None, str | None, str]] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    def forget_cache(self) -> None:
        self._validators.clear()

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        attempt = 0
        while True:
            try:
                async with self._hosts[host], self._global:
                    resp = await self.client.request(method, url, **kw)
                if resp.status_code not in RETRY_STATUS or attempt >= self.cfg.retries:
                    return resp
                delay = _retry_after(resp) or (2 ** attempt + random.random())
                log.info("retry %s %s in %.1fs (HTTP %s)", method, url, delay, resp.status_code)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= self.cfg.retries:
                    raise FetchError(f"{type(e).__name__}: {e}") from e
                delay = 2 ** attempt + random.random()
            attempt += 1
            await asyncio.sleep(min(delay, 60))

    async def get_json(self, url: str, *, params: dict | None = None, detect_unchanged: bool = False,
                       headers: dict | None = None) -> tuple[Any, bool]:
        """Returns (data, unchanged). When unchanged is True data is None."""
        key = str(httpx.URL(url, params=params))
        h = {"Accept": "application/json", **(headers or {})}
        cached = self._validators.get(key) if detect_unchanged else None
        if cached:
            if cached[0]:
                h["If-None-Match"] = cached[0]
            if cached[1]:
                h["If-Modified-Since"] = cached[1]
        resp = await self._request("GET", url, params=params, headers=h)
        if resp.status_code == 304 and cached:
            return None, True
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {key}", resp.status_code)
        body = resp.content
        if detect_unchanged:
            digest = hashlib.sha1(body).hexdigest()
            if cached and cached[2] == digest:
                return None, True
            self._validators[key] = (resp.headers.get("etag"), resp.headers.get("last-modified"), digest)
        try:
            return json.loads(body), False
        except json.JSONDecodeError as e:
            raise FetchError(f"invalid JSON from {key}: {e}") from e

    async def post_json(self, url: str, body: Any, *, headers: dict | None = None) -> Any:
        resp = await self._request("POST", url, json=body, headers={"Accept": "application/json", **(headers or {})})
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for POST {url}", resp.status_code)
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise FetchError(f"invalid JSON from {url}: {e}") from e

    async def get_page(self, url: str, max_bytes: int = 600_000) -> PageResult:
        """GET an HTML page (for liveness checks), reading at most max_bytes."""
        host = urlsplit(url).hostname or ""
        try:
            async with self._hosts[host], self._global:
                async with self.client.stream("GET", url, headers={"Accept": "text/html,*/*;q=0.8"}) as resp:
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= max_bytes:
                            break
                    text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                    return PageResult(resp.status_code, str(resp.url), text)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e

    async def status_of(self, url: str) -> int:
        resp = await self._request("GET", url, headers={"Accept": "application/json"})
        return resp.status_code


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if v and v.isdigit():
        return float(v)
    return None
