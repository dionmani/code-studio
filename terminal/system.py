"""WebSocket gateway for the System terminal: a real, unrestricted shell.

Unlike the scoped terminal (consumers.py) this is one long-lived shell in a
pseudo terminal, with the user's profile, real paths and full environment
(minus Code Studio's own variables). It exists for a local copy where the IDE
user *is* the machine's user, so it is only served when
``CODE_STUDIO["SYSTEM_TERMINAL"]`` is on **and** the client is on loopback.

Connect with ``?shell=<name>&cols=<n>&rows=<n>``; the shell starts in the
project root. Protocol (JSON messages):
  client -> {"type": "input", "data": "..."} | {"type": "resize", "cols": n, "rows": n} | {"type": "ping"}
  server -> {"type": "ready", "shell": "...", "shells": [...], "cwd": "C:\\..."}
            {"type": "output", "data": "..."}
            {"type": "exit", "code": n}            (then the socket closes)
            {"type": "error", "message": "..."}
Close codes: 4401 not signed in, 4403 disabled / not loopback, 4404 no such project.
"""
from __future__ import annotations

import asyncio
import codecs
import ipaddress
import threading
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from .consumers import _audit, _load_project
from .syspty import PtySession, available_shells, shell_environment

MAX_INPUT = 64 * 1024


def system_terminal_enabled() -> bool:
    return bool(settings.CODE_STUDIO.get("SYSTEM_TERMINAL"))


def is_loopback(host) -> bool:
    try:
        return ipaddress.ip_address(str(host or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def _int(q, key, default, lo, hi):
    try:
        return max(lo, min(int(q.get(key, [default])[0]), hi))
    except (TypeError, ValueError):
        return default


class SystemTerminalConsumer(AsyncJsonWebsocketConsumer):
    session: PtySession | None = None
    project = None

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return
        if not system_terminal_enabled() or not is_loopback((self.scope.get("client") or [None])[0]):
            await self.close(code=4403)
            return
        self.user = user
        try:
            self.project, self.root = await _load_project(user, self.scope["url_route"]["kwargs"]["project_id"])
        except Exception:
            self.project = None
            await self.close(code=4404)
            return
        q = parse_qs(self.scope.get("query_string", b"").decode("latin-1"))
        shells = available_shells()
        self.shell = q.get("shell", [""])[0]
        if self.shell not in shells:
            self.shell = next(iter(shells))
        cols, rows = _int(q, "cols", 120, 2, 1000), _int(q, "rows", 30, 1, 500)

        await self.accept()
        try:
            self.session = await asyncio.to_thread(PtySession, shells[self.shell], str(self.root),
                                                   shell_environment(), cols, rows)
        except OSError as exc:
            await self.send_json({"type": "error", "message": f"Failed to start {self.shell}: {exc}"})
            await self.close(code=1011)
            return
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pump = asyncio.create_task(self._pump())
        await _audit(user, self.project, "terminal.system_start", target=str(self.root), shell=self.shell)
        await self.send_json({"type": "ready", "shell": self.shell, "shells": list(shells), "cwd": str(self.root)})
        threading.Thread(target=self._reader, args=(self.session,), daemon=True).start()

    def _reader(self, session: PtySession):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = session.read()
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, {"type": "output", "data": text})
        tail = decoder.decode(b"", final=True)
        if tail:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, {"type": "output", "data": tail})
        self.loop.call_soon_threadsafe(self.queue.put_nowait, None)  # EOF

    async def _pump(self):
        while True:
            msg = await self.queue.get()
            if msg is None:
                break
            # Coalesce bursts (e.g. a long directory listing) into fewer frames.
            while msg["type"] == "output" and not self.queue.empty():
                nxt = self.queue.get_nowait()
                if nxt is None:
                    await self.send_json(msg)
                    msg = None
                    break
                msg["data"] += nxt["data"]
                if len(msg["data"]) > 256 * 1024:
                    break
            if msg is None:
                break
            await self.send_json(msg)
        session = self.session
        code = None
        if session is not None:
            for _ in range(20):  # the exit code lands just after EOF
                code = session.exit_code
                if code is not None:
                    break
                await asyncio.sleep(0.05)
        await self.send_json({"type": "exit", "code": code})
        await self._finish(close_code=1000)
        await self.close(code=1000)

    async def receive_json(self, content, **kwargs):
        session = self.session
        if session is None:
            return
        kind = content.get("type")
        if kind == "input":
            data = str(content.get("data", ""))[:MAX_INPUT]
            if data:
                try:
                    await asyncio.to_thread(session.write, data.encode("utf-8"))
                except OSError:
                    pass
        elif kind == "resize":
            try:
                await asyncio.to_thread(session.resize, int(content.get("cols", 0)), int(content.get("rows", 0)))
            except (TypeError, ValueError, OSError):
                pass
        elif kind == "ping":
            await self.send_json({"type": "pong"})

    async def _finish(self, close_code):
        """Kill the shell tree and audit the end; safe to call more than once."""
        session, self.session = self.session, None
        if session is None:
            return
        exit_code = session.exit_code
        await asyncio.to_thread(session.close)
        await _audit(self.user, self.project, "terminal.system_end", shell=self.shell,
                     exit_code=exit_code, code=close_code)

    async def disconnect(self, code):
        await self._finish(code)
        task = getattr(self, "pump", None)
        if task and task is not asyncio.current_task():
            task.cancel()
