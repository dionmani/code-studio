"""HTTP client for the Agent Engine contract v1 (signed requests)."""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx
from django.conf import settings

from cs_common import signing
from cs_common.datadir import engine_token


class EngineError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class EngineUnavailable(EngineError):
    def __init__(self, message: str = "Agent Engine is offline"):
        super().__init__(message, 503)


_health_cache: dict = {"at": 0.0, "value": None}


class EngineClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float | None = None):
        cfg = settings.CODE_STUDIO
        self.base_url = (base_url or cfg["ENGINE_URL"]).rstrip("/")
        self.token = token or engine_token(settings.DATA_DIR)
        self.timeout = timeout or cfg["ENGINE_TIMEOUT"]

    def _signed(self, method: str, path: str, body: bytes = b"") -> httpx.Request:
        req = httpx.Request(method, self.base_url + path, content=body or None,
                            headers={"Content-Type": "application/json"} if body else {})
        raw = req.url.raw_path.decode("latin-1")
        req.headers.update(signing.sign(self.token, method, raw, body))
        return req

    def request(self, method: str, path: str, payload: dict | None = None, timeout: float | None = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                resp = client.send(self._signed(method, path, body))
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"Agent Engine unreachable: {exc.__class__.__name__}")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail") or resp.json().get("error")
            except ValueError:
                detail = resp.text[:300]
            raise EngineError(f"Engine error {resp.status_code}: {detail}", resp.status_code)
        return resp.json()

    def health(self, use_cache: bool = True) -> dict:
        now = time.time()
        if use_cache and _health_cache["value"] is not None and now - _health_cache["at"] < 3:
            if _health_cache["base"] == self.base_url:
                return _health_cache["value"]
        try:
            value = {"online": True, **self.request("GET", "/health", timeout=3)}
        except EngineError as exc:
            value = {"online": False, "error": str(exc)}
        _health_cache.update(at=now, value=value, base=self.base_url)
        return value

    def agents(self) -> dict:
        return self.request("GET", "/agents", timeout=60)

    def set_agent_enabled(self, identifier: str, enabled: bool) -> dict:
        from urllib.parse import quote

        return self.request("PUT", f"/agents/{quote(identifier, safe='')}", {"enabled": bool(enabled)}, timeout=60)

    def submit(self, payload: dict) -> dict:
        # images can make the body tens of MB: allow time for the engine to validate and store them
        return self.request("POST", "/tasks", payload, timeout=60 if payload.get("images") else None)

    def task(self, task_id: str) -> dict:
        return self.request("GET", f"/tasks/{task_id}")

    def cancel(self, task_id: str) -> dict:
        return self.request("POST", f"/tasks/{task_id}/cancel", {})

    def followup(self, task_id: str, payload: dict) -> dict:
        return self.request("POST", f"/tasks/{task_id}/followup", payload,
                            timeout=60 if payload.get("images") else None)

    def changes(self, task_id: str) -> dict:
        return self.request("GET", f"/tasks/{task_id}/changes", timeout=60)

    def approve(self, task_id: str, payload: dict) -> dict:
        return self.request("POST", f"/tasks/{task_id}/approve", payload)

    def events(self, task_id: str, after: int = 0) -> dict:
        return self.request("GET", f"/tasks/{task_id}/events?after={int(after)}&follow=0")

    async def stream_events(self, task_id: str, after: int = 0) -> AsyncIterator[dict]:
        """Yield parsed SSE events ({'event':..., 'data': {...}}) from the engine."""
        req = self._signed("GET", f"/tasks/{task_id}/events?after={int(after)}&follow=1")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=60)) as client:
                resp = await client.send(req, stream=True)
                if resp.status_code >= 400:
                    raise EngineError(f"Engine error {resp.status_code}", resp.status_code)
                event, data = "message", []
                async for line in resp.aiter_lines():
                    if line == "":
                        if data:
                            try:
                                yield {"event": event, "data": json.loads("\n".join(data))}
                            except ValueError:
                                pass
                        event, data = "message", []
                    elif line.startswith(":"):
                        yield {"event": "keepalive", "data": {}}
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"Agent Engine stream failed: {exc.__class__.__name__}")
