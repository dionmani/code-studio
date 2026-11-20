"""WebSocket terminal gateway, locked to one project's canonical root.

Protocol (JSON messages):
  client -> {"type": "run", "command": "..."} | {"type": "cancel"} | {"type": "ping"}
  server -> {"type": "ready", "cwd": "...", "shell": "..."}
            {"type": "output", "data": "..."}
            {"type": "exit", "code": 0, "cwd": "..."}
            {"type": "denied", "reason": "..."}
            {"type": "error", "message": "..."}
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from audit.log import record
from cs_common.proc import kill_tree, popen_kwargs
from cs_common.redact import Redactor, sanitize_env
from projects.paths import PathSecurityError, is_within, project_root, to_relative

from .policy import check_command, resolve_cd

if os.name == "nt":
    SHELLS = {
        "powershell": ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"],
        "cmd": ["cmd.exe", "/d", "/s", "/c"],
    }
else:  # Linux / Docker
    SHELLS = {"bash": ["bash", "--noprofile", "--norc", "-c"], "sh": ["sh", "-c"]}
DEFAULT_SHELL = next(iter(SHELLS))
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "


def _cfg(key):
    return settings.CODE_STUDIO[key]


@database_sync_to_async
def _load_project(user, project_id):
    from projects.service import get_project_for

    project = get_project_for(user, project_id)
    return project, project_root(project)


@database_sync_to_async
def _audit(user, project, action, target="", outcome="success", **detail):
    record(actor=user, action=action, project=project, target=target, outcome=outcome, **detail)


class TerminalConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        try:
            self.project, self.root = await _load_project(user, self.scope["url_route"]["kwargs"]["project_id"])
        except Exception:
            await self.close(code=4404)
            return
        self.cwd: Path = self.root
        self.proc: subprocess.Popen | None = None
        self.shell = DEFAULT_SHELL
        self.redactor = Redactor()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self.last_activity = time.monotonic()
        await self.accept()
        self.pump = asyncio.create_task(self._pump())
        self.idle = asyncio.create_task(self._idle_watch())
        await _audit(user, self.project, "terminal.session_start", target=str(self.root))
        await self.send_json({"type": "ready", "cwd": self._rel_cwd(), "shell": self.shell,
                              "root": self.project.name, "shells": list(SHELLS)})

    def _rel_cwd(self) -> str:
        rel = to_relative(self.root, self.cwd)
        return "~/" + rel if rel else "~"

    async def disconnect(self, code):
        if getattr(self, "proc", None) is not None:
            await asyncio.to_thread(kill_tree, self.proc)
        for name in ("pump", "idle"):
            task = getattr(self, name, None)
            if task:
                task.cancel()
        if getattr(self, "project", None) is not None:
            await _audit(self.user, self.project, "terminal.session_end", code=code)

    async def _pump(self):
        while True:
            msg = await self.queue.get()
            await self.send_json(msg)

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(30)
            if self.proc is None and time.monotonic() - self.last_activity > _cfg("TERMINAL_IDLE_TIMEOUT"):
                await self.send_json({"type": "error", "message": "Session closed after inactivity"})
                await self.close(code=4408)
                return

    def _emit(self, msg):  # thread-safe
        self.loop.call_soon_threadsafe(self.queue.put_nowait, msg)

    async def receive_json(self, content, **kwargs):
        self.last_activity = time.monotonic()
        kind = content.get("type")
        if kind == "ping":
            await self.send_json({"type": "pong"})
        elif kind == "cancel":
            if self.proc is not None:
                await asyncio.to_thread(kill_tree, self.proc)
                await _audit(self.user, self.project, "terminal.cancel", outcome="cancelled")
        elif kind == "shell" and content.get("shell") in SHELLS and self.proc is None:
            self.shell = content["shell"]
            await self.send_json({"type": "ready", "cwd": self._rel_cwd(), "shell": self.shell,
                                  "root": self.project.name, "shells": list(SHELLS)})
        elif kind == "run":
            await self._run(str(content.get("command", "")))
        else:
            await self.send_json({"type": "error", "message": "Unknown message"})

    async def _run(self, command: str):
        command = command.strip()
        if self.proc is not None:
            await self.send_json({"type": "error", "message": "A command is already running (press Stop)"})
            return
        if not command:
            await self.send_json({"type": "exit", "code": 0, "cwd": self._rel_cwd()})
            return
        try:  # re-validate the root on every command (junction swaps etc.)
            root = await database_sync_to_async(project_root)(self.project)
            if os.path.normcase(str(root)) != os.path.normcase(str(self.root)) or not is_within(self.cwd, root):
                raise PathSecurityError("Project root changed")
        except PathSecurityError as exc:
            await self.send_json({"type": "error", "message": str(exc)})
            await self.close(code=4403)
            return

        lowered = command.lower()
        if lowered == "cd" or lowered.startswith("cd ") or lowered.startswith("cd\\") or lowered == "cd..":
            arg = command[2:].strip()
            target, err = resolve_cd(arg, self.cwd, self.root)
            if target is None:
                await _audit(self.user, self.project, "terminal.command", target=command, outcome="denied", reason=err)
                await self.send_json({"type": "denied", "reason": err})
                await self.send_json({"type": "exit", "code": 1, "cwd": self._rel_cwd()})
                return
            self.cwd = target
            await _audit(self.user, self.project, "terminal.command", target=command)
            await self.send_json({"type": "exit", "code": 0, "cwd": self._rel_cwd()})
            return
        if lowered in ("pwd", "get-location"):
            await self.send_json({"type": "output", "data": self._rel_cwd() + "\r\n"})
            await self.send_json({"type": "exit", "code": 0, "cwd": self._rel_cwd()})
            return

        decision = check_command(command, self.cwd, self.root)
        if not decision:
            await _audit(self.user, self.project, "terminal.command", target=command, outcome="denied",
                         reason=decision.reason)
            await self.send_json({"type": "denied", "reason": decision.reason})
            await self.send_json({"type": "exit", "code": 126, "cwd": self._rel_cwd()})
            return
        await _audit(self.user, self.project, "terminal.command", target=command, shell=self.shell,
                     cwd=self._rel_cwd())
        argv = SHELLS[self.shell] + [(PS_PREFIX + command) if self.shell == "powershell" else command]
        # Code Studio's own Django settings must not leak into the user's project
        # (a project's manage.py uses os.environ.setdefault and would load ours).
        env = sanitize_env(drop=("DJANGO_SETTINGS_MODULE",))
        env["CODE_STUDIO_PROJECT_ROOT"] = str(self.root)
        try:
            self.proc = subprocess.Popen(argv, cwd=str(self.cwd), env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kwargs())
        except OSError as exc:
            self.proc = None
            await self.send_json({"type": "error", "message": f"Failed to start shell: {exc}"})
            await self.send_json({"type": "exit", "code": 127, "cwd": self._rel_cwd()})
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen):
        limit = _cfg("TERMINAL_MAX_OUTPUT")
        deadline = time.monotonic() + _cfg("TERMINAL_COMMAND_TIMEOUT")
        total = 0
        timed_out = False

        def watchdog():
            nonlocal timed_out
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    timed_out = True
                    kill_tree(proc)
                    return
                time.sleep(0.5)

        threading.Thread(target=watchdog, daemon=True).start()
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            total += len(chunk)
            if total <= limit:
                self._emit({"type": "output", "data": self.redactor.redact(chunk.decode("utf-8", errors="replace"))})
            elif total - len(chunk) <= limit:
                self._emit({"type": "output", "data": "\r\n[output truncated]\r\n"})
        code = proc.wait()
        if timed_out:
            self._emit({"type": "error", "message": "Command timed out and was terminated"})
        self.proc = None
        self._emit({"type": "exit", "code": code, "cwd": self._rel_cwd()})
