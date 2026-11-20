"""Adapter interface + a generic subprocess-backed implementation.

Every adapter implements the operations required by PROJECT_DESIGN.md §8:
``probe()``, ``start_task()``, ``stream_events()``, ``cancel()``, ``get_result()``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from cs_common.proc import kill_tree, popen_kwargs
from cs_common.redact import Redactor, agent_env, sanitize_env

MAX_EVENT_TEXT = 8000
MAX_RESULT_TEXT = 60_000  # an agent's final reply (kept whole for the task summary)
MAX_LOG_BYTES = 20 * 1024 * 1024


class AdapterUnavailable(Exception):
    pass


@dataclass
class ProbeResult:
    identifier: str
    display_name: str
    available: bool
    version: str | None = None
    executable: str | None = None
    auth_state: str = "unknown"
    capabilities: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # None: this adapter has no on/off switch. True/False: the switch's current value
    # (changed at runtime with PUT /agents/{identifier}).
    user_enabled: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunState:
    id: str
    proc: subprocess.Popen | None = None
    events: list[dict] = field(default_factory=list)
    status: str = "running"  # running | succeeded | failed | cancelled | timed_out
    exit_code: int | None = None
    result_text: str = ""
    result_stats: dict = field(default_factory=dict)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)
    log_path: Path | None = None
    log_bytes: int = 0


# Event payload keys an adapter uses to report the CLI's conversation id
# (Claude Code: session_id, Codex: thread_id, Antigravity: conversation_id).
SESSION_KEYS = ("session_id", "thread_id", "conversation_id")

RESULT_STATS = ("num_turns", "duration_ms", "cost_usd", "is_error", "subtype", "checks_total", "checks_failed")


def _trunc(text: str, n: int = MAX_EVENT_TEXT) -> str:
    return text if len(text) <= n else text[:n] + f"... [truncated {len(text) - n} chars]"


class BaseAdapter:
    identifier = "base"
    display_name = "Base"
    executable_names: tuple[str, ...] = ()
    env_keep: tuple[str, ...] = ()
    env_drop: tuple[str, ...] = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
    _probe_ttl = 60.0
    # Adapters with an on/off switch an admin flips at runtime (stored in the engine's SettingsStore).
    toggleable = False
    enabled_by_default = False
    # Can continue an earlier run's conversation: build_command() honours
    # policy["resume_session"] (the id the CLI reported, see SESSION_KEYS).
    supports_resume = False
    # Sends the user's images to the model itself (policy["images"]: absolute paths). Otherwise the
    # prompt only lists the image files in the handoff folder for the agent to open.
    attaches_images = False

    def __init__(self, settings=None):
        self._runs: dict[str, RunState] = {}
        self._probe_cache: tuple[float, ProbeResult] | None = None
        self._lock = threading.Lock()
        self.settings = settings  # agent_engine.settings_store.SettingsStore | None

    # ---- runtime on/off switch ----------------------------------------
    def is_enabled(self) -> bool:
        if not self.toggleable:
            return True
        if self.settings is None:
            return self.enabled_by_default
        return bool(self.settings.adapter(self.identifier).get("enabled", self.enabled_by_default))

    def set_enabled(self, enabled: bool) -> None:
        if not self.toggleable:
            raise ValueError(f"{self.display_name} has no on/off switch")
        if self.settings is None:
            raise ValueError("engine settings are not available")
        self.settings.update_adapter(self.identifier, enabled=bool(enabled))
        self.invalidate_probe()

    def invalidate_probe(self) -> None:
        self._probe_cache = None

    # ---- discovery ----------------------------------------------------
    def find_executable(self) -> str | None:
        for name in self.executable_names:
            found = shutil.which(name)
            if found:
                return found
        return None

    def probe(self) -> ProbeResult:
        now = time.time()
        if self._probe_cache and now - self._probe_cache[0] < self._probe_ttl:
            return self._probe_cache[1]
        result = self._probe()
        if self.toggleable:
            result.user_enabled = self.is_enabled()
        self._probe_cache = (now, result)
        return result

    def _probe(self) -> ProbeResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def _run_quick(self, argv: list[str], timeout: float = 20) -> tuple[int, str]:
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                                errors="replace", env=sanitize_env(keep=self.env_keep, drop=self.env_drop),
                                stdin=subprocess.DEVNULL, **popen_kwargs())
            return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, str(exc)

    # ---- execution ----------------------------------------------------
    def build_command(self, prompt: str, workspace: Path, policy: dict) -> tuple[list[str], str]:
        """Return (argv, stdin_text). Prompts go over stdin, never the command line."""
        raise NotImplementedError

    def max_prompt_chars(self) -> int | None:
        """Longest prompt this CLI can take (None: no practical limit, e.g. prompts sent on stdin)."""
        return None

    def prompt_notes(self, policy: dict) -> str:
        """Adapter-specific facts the agent should know (e.g. which commands it may run)."""
        return ""

    def parse_line(self, line: str) -> list[dict]:
        """Translate one stdout line to zero or more {type, payload} events."""
        return [{"type": "agent.output", "payload": {"text": _trunc(line)}}]

    def start_task(self, task: dict, context: str, workspace: Path, policy: dict,
                   log_path: Path | None = None) -> str:
        probe = self.probe()
        if not probe.available:
            raise AdapterUnavailable(f"{self.display_name} is not available: {'; '.join(probe.notes)}")
        prompt = context
        argv, stdin_text = self.build_command(prompt, workspace, policy)
        run = RunState(id=uuid.uuid4().hex, log_path=log_path)
        env = self.process_env(workspace, policy)
        redactor = Redactor()
        try:
            run.proc = subprocess.Popen(argv, cwd=str(workspace), env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())
        except OSError as exc:
            raise AdapterUnavailable(f"Failed to start {self.display_name}: {exc}")
        with self._lock:
            self._runs[run.id] = run
        self._push(run, {"type": "agent.started", "payload": {"adapter": self.identifier, "pid": run.proc.pid,
                                                               "argv": [Path(argv[0]).name] + argv[1:]}})

        def feed():
            try:
                run.proc.stdin.write(stdin_text.encode("utf-8"))
                run.proc.stdin.close()
            except OSError:
                pass

        def read_stdout():
            for raw in iter(run.proc.stdout.readline, b""):
                line = redactor.redact(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                self._log(run, "stdout", line)
                if not line.strip():
                    continue
                for ev in self.parse_line(line):
                    if ev.get("type") == "agent.result":
                        payload = ev["payload"] = dict(ev.get("payload") or {})
                        if payload.get("text"):
                            run.result_text = _trunc(str(payload["text"]), MAX_RESULT_TEXT)
                            payload["text"] = _trunc(run.result_text)  # the live log gets a shorter copy
                        run.result_stats = {k: payload[k] for k in RESULT_STATS if payload.get(k) is not None}
                    self._push(run, ev)

        def read_stderr():
            for raw in iter(run.proc.stderr.readline, b""):
                line = redactor.redact(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                self._log(run, "stderr", line)
                if line.strip():
                    self._push(run, {"type": "agent.stderr", "payload": {"text": _trunc(line, 2000)}})

        timeout = float(policy.get("timeout_seconds") or 900)

        def supervise():
            t_out = threading.Thread(target=read_stdout, daemon=True)
            t_err = threading.Thread(target=read_stderr, daemon=True)
            t_out.start()
            t_err.start()
            threading.Thread(target=feed, daemon=True).start()
            try:
                run.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                run.status = "timed_out"
                run.error = f"Timed out after {int(timeout)}s"
                kill_tree(run.proc)
            t_out.join(timeout=10)
            t_err.join(timeout=10)
            run.exit_code = run.proc.returncode
            if run.status == "running":
                run.status = "succeeded" if run.exit_code == 0 else "failed"
                if run.exit_code != 0 and not run.error:
                    run.error = f"Process exited with code {run.exit_code}"
            run.finished_at = time.time()
            self._push(run, {"type": "agent.finished", "payload": {"status": run.status, "exit_code": run.exit_code,
                                                                    "error": run.error}}, final=True)

        threading.Thread(target=supervise, daemon=True).start()
        return run.id

    def process_env(self, workspace: Path, policy: dict) -> dict:
        """The child's environment: no secrets or database settings, the project's forced
        variables (e.g. its test settings) and its Python environment first on PATH."""
        env = agent_env(keep=self.env_keep, drop=self.env_drop)
        env.update(policy.get("env") or {})
        venv_bin = policy.get("python_env_bin")
        if venv_bin:
            env["VIRTUAL_ENV"] = str(Path(venv_bin).parent)
            env.pop("PYTHONHOME", None)
            path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
            env[path_key] = str(venv_bin) + os.pathsep + env.get(path_key, "")
        env["CODE_STUDIO_WORKSPACE"] = str(workspace)
        return env

    def _log(self, run: RunState, stream: str, line: str):
        if run.log_path is None or run.log_bytes > MAX_LOG_BYTES:
            return
        data = f"[{stream}] {line}\n".encode("utf-8")
        run.log_bytes += len(data)
        with open(run.log_path, "ab") as fh:
            fh.write(data if run.log_bytes <= MAX_LOG_BYTES else b"[log truncated]\n")

    def _push(self, run: RunState, event: dict, final: bool = False):
        event = dict(event)
        event["ts"] = time.time()
        with run.cond:
            run.events.append(event)
            if final:
                run.events.append({"type": "__end__", "ts": time.time()})
            run.cond.notify_all()

    def _get(self, run_id: str) -> RunState:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def stream_events(self, run_id: str) -> Iterator[dict]:
        run = self._get(run_id)
        idx = 0
        while True:
            with run.cond:
                while idx >= len(run.events):
                    run.cond.wait(timeout=1.0)
                batch = run.events[idx:]
                idx = len(run.events)
            for ev in batch:
                if ev["type"] == "__end__":
                    return
                yield ev

    def cancel(self, run_id: str) -> dict:
        run = self._get(run_id)
        if run.proc is not None and run.proc.poll() is None:
            run.status = "cancelled"
            run.error = "Cancelled by user"
            kill_tree(run.proc)
            return {"cancelled": True}
        return {"cancelled": False, "status": run.status}

    def get_result(self, run_id: str) -> dict:
        run = self._get(run_id)
        return {"status": run.status, "exit_code": run.exit_code, "result": run.result_text, "error": run.error,
                "started_at": run.started_at, "finished_at": run.finished_at, "stats": dict(run.result_stats)}


def parse_json_line(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
