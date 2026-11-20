"""Agent Engine HTTP API (contract v1). See docs/ENGINE_API.md."""
from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from cs_common import signing

from . import generate
from .adapters import AdapterUnavailable, build_registry
from .adapters.antigravity import ENABLE_ENV as ANTIGRAVITY_ENV
from .config import EngineConfig
from .settings_store import SettingsStore
from .tasks import TERMINAL, TaskManager

ENGINE_VERSION = "0.1.0"


def create_app(config: EngineConfig | None = None) -> FastAPI:
    config = config or EngineConfig.from_env()
    # Runtime settings (adapter on/off switches). The legacy env switch only seeds a new file.
    settings = SettingsStore(config.engine_dir / "settings.json",
                             seed={"antigravity": {"enabled": os.environ.get(ANTIGRAVITY_ENV) == "1"}})
    adapters = build_registry(config.enable_test_adapter, settings)
    manager = TaskManager(config, adapters)
    app = FastAPI(title="Code Studio Agent Engine", version=ENGINE_VERSION, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.config = config
    app.state.manager = manager

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        body = await request.body()
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        qs = request.scope.get("query_string") or b""
        full = raw_path.decode("latin-1") + (("?" + qs.decode("latin-1")) if qs else "")
        ok = signing.verify(config.token, request.method, full, body, request.headers.get(signing.HEADER_TS),
                            request.headers.get(signing.HEADER_SIG))
        if not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        version = request.headers.get(signing.HEADER_VERSION)
        if version and version != signing.API_VERSION:
            return JSONResponse({"error": f"unsupported API version {version}",
                                 "supported": signing.API_VERSION}, status_code=400)
        response = await call_next(request)
        response.headers[signing.HEADER_VERSION] = signing.API_VERSION
        return response

    def get_task(task_id: str):
        task = manager.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return task

    @app.get("/health")
    async def health():
        running = sum(1 for t in manager.tasks.values() if t.state not in TERMINAL)
        return {"status": "ok", "api_version": signing.API_VERSION, "engine_version": ENGINE_VERSION,
                "engine": "code-studio-engine", "tasks_running": running, "time": time.time()}

    @app.get("/agents")
    async def agents():
        probes = await asyncio.gather(*(asyncio.to_thread(a.probe) for a in adapters.values()))
        return {"agents": [p.to_dict() for p in probes]}

    @app.put("/agents/{identifier}")
    async def update_agent(identifier: str, request: Request):
        """Turn a switchable adapter on/off. Takes effect for new tasks immediately (no restart)."""
        adapter = adapters.get(identifier)
        if adapter is None:
            raise HTTPException(404, "unknown agent")
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(400, "invalid JSON body")
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise HTTPException(400, 'body must be {"enabled": true|false}')
        try:
            adapter.set_enabled(payload["enabled"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        probe = await asyncio.to_thread(adapter.probe)
        return {"agent": probe.to_dict()}

    @app.post("/generate")
    async def generate_text(request: Request):
        """One-shot, read-only text generation (no workspace, no review). See generate.py."""
        try:
            payload = json.loads(await request.body() or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            return await asyncio.to_thread(generate.generate, config.engine_dir, adapters, payload)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except AdapterUnavailable as exc:
            raise HTTPException(409, str(exc))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/tasks", status_code=201)
    async def submit(request: Request):
        try:
            payload = json.loads(await request.body() or b"{}")
            task = await asyncio.to_thread(manager.submit, payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        return task.public()

    @app.get("/tasks")
    async def list_tasks(project_id: int | None = None):
        tasks = [t.public() for t in manager.tasks.values()
                 if project_id is None or (t.data.get("project") or {}).get("id") == project_id]
        tasks.sort(key=lambda t: t["created_at"], reverse=True)
        return {"tasks": tasks[:200]}

    @app.get("/tasks/{task_id}")
    async def task_detail(task_id: str):
        return get_task(task_id).public()

    @app.get("/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request, after: int = 0, follow: int = 1):
        task = get_task(task_id)
        if not follow:
            return {"events": manager.events_after(task, after), "state": task.state}

        async def gen():
            last = after
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                batch = manager.events_after(task, last)
                for ev in batch:
                    last = ev["seq"]
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
                if not batch:
                    if task.state in TERMINAL:
                        yield f"event: end\ndata: {json.dumps({'state': task.state, 'last_seq': last})}\n\n"
                        return
                    idle += 0.25
                    if idle >= 15:
                        idle = 0
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.25)
                else:
                    idle = 0

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        task = get_task(task_id)
        return await asyncio.to_thread(manager.cancel, task)

    @app.post("/tasks/{task_id}/followup")
    async def followup(task_id: str, request: Request):
        """Continue one step's conversation with a new message, in the task's staging copy."""
        task = get_task(task_id)
        try:
            payload = json.loads(await request.body() or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            task = await asyncio.to_thread(manager.followup, task, payload)
        except LookupError as exc:
            raise HTTPException(409, str(exc))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        return task.public()

    @app.get("/tasks/{task_id}/changes")
    async def changes(task_id: str):
        task = get_task(task_id)
        return {"task_id": task_id, "state": task.state, "base_revision": task.data.get("base_revision"),
                "changes": await asyncio.to_thread(manager.changes, task)}

    @app.post("/tasks/{task_id}/approve")
    async def approve(task_id: str, request: Request):
        """Record the IDE's validated decision. The engine never writes the live project."""
        task = get_task(task_id)
        try:
            payload = json.loads(await request.body() or b"{}")
            return await asyncio.to_thread(manager.decide, task, payload.get("decision", "applied"),
                                           list(payload.get("files") or []), payload.get("actor"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc))

    return app
