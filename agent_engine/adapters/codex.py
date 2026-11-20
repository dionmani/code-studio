"""OpenAI Codex CLI adapter (verified against codex-cli 0.169 on Windows).

Headless interface: ``codex exec --json --skip-git-repo-check -C <ws>
--sandbox workspace-write|read-only -`` with the prompt on stdin. A follow-up
continues the thread with ``codex exec resume ... <thread_id> -`` (the id comes
from the ``thread.started`` record). Images the user attached are passed with
``--image=<path>`` (one flag per image, ``=`` form so the multi-value option
never swallows the ``-`` that means "prompt on stdin").
On Windows the npm shim (codex.cmd) is bypassed: we run ``node codex.js``
directly so no batch-file argument parsing is involved.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .base import MAX_RESULT_TEXT, BaseAdapter, ProbeResult, _trunc, parse_json_line


class CodexAdapter(BaseAdapter):
    identifier = "codex"
    display_name = "OpenAI Codex CLI"
    executable_names = ("codex",)
    env_keep = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")
    supports_resume = True
    attaches_images = True  # --image: the model sees the pictures with the prompt

    def _argv0(self) -> list[str] | None:
        exe = self.find_executable()
        if not exe:
            return None
        p = Path(exe)
        if p.suffix.lower() in (".cmd", ".bat", "") and (p.parent / "node_modules/@openai/codex/bin/codex.js").is_file():
            node = shutil.which("node")
            if node:
                return [node, str(p.parent / "node_modules/@openai/codex/bin/codex.js")]
        return [exe]

    def _probe(self) -> ProbeResult:
        argv0 = self._argv0()
        res = ProbeResult(self.identifier, self.display_name, available=False,
                          executable=" ".join(argv0) if argv0 else None,
                          capabilities={"headless": True, "streaming": True, "staged_edits": True,
                                        "read_only": True, "cancel": True, "model_select": True,
                                        "resume": True})
        if not argv0:
            res.notes.append("'codex' executable not found on PATH")
            res.capabilities = {}
            return res
        code, out = self._run_quick(argv0 + ["--version"])
        if code != 0:
            res.notes.append(f"'codex --version' failed: {out.strip()[:200]}")
            return res
        res.version = out.strip().splitlines()[-1] if out.strip() else None
        code, out = self._run_quick(argv0 + ["login", "status"])
        res.auth_state = "logged_in" if code == 0 and "logged in" in out.lower() else "logged_out"
        if res.auth_state == "logged_out":
            res.notes.append("Codex is installed but not logged in (run 'codex login')")
        res.available = res.auth_state == "logged_in"
        return res

    def build_command(self, prompt: str, workspace: Path, policy: dict):
        sandbox = "read-only" if policy.get("read_only") else "workspace-write"
        images = [f"--image={p}" for p in policy.get("images") or []]
        if policy.get("resume_session"):
            # ``codex exec resume`` has no -C/--sandbox flags: the cwd is the staging copy
            # (set by start_task) and the sandbox is given as a config override.
            argv = self._argv0() + ["exec", "resume", *images, "--json", "--skip-git-repo-check",
                                    "-c", f'sandbox_mode="{sandbox}"']
            if policy.get("model"):
                argv += ["-m", str(policy["model"])]
            return argv + [str(policy["resume_session"]), "-"], prompt
        argv = self._argv0() + ["exec", *images, "--json", "--skip-git-repo-check", "-C", str(workspace),
                                "--sandbox", sandbox]
        if policy.get("model"):
            argv += ["-m", str(policy["model"])]
        argv.append("-")
        return argv, prompt

    def parse_line(self, line: str) -> list[dict]:
        obj = parse_json_line(line)
        if obj is None:
            return [{"type": "agent.output", "payload": {"text": _trunc(line)}}]
        t = obj.get("type", "")
        item = obj.get("item") or {}
        itype = item.get("type")
        if t in ("item.completed", "item.started", "item.updated") and itype:
            if itype == "agent_message":
                if t != "item.completed":
                    return []
                return [{"type": "agent.message", "payload": {"text": _trunc(item.get("text", ""))}},
                        {"type": "agent.result", "payload": {"text": _trunc(item.get("text", ""), MAX_RESULT_TEXT)}}]
            if itype == "reasoning":
                return [{"type": "agent.thinking", "payload": {"text": _trunc(item.get("text", ""), 2000)}}] \
                    if t == "item.completed" else []
            if itype == "command_execution":
                return [{"type": "agent.tool_use" if t == "item.started" else "agent.tool_result", "payload": {
                    "tool": "shell", "input": _trunc(str(item.get("command", "")), 2000),
                    "text": _trunc(str(item.get("aggregated_output", "")), 2000),
                    "exit_code": item.get("exit_code"), "status": item.get("status")}}]
            if itype == "file_change":
                return [{"type": "agent.file_change", "payload": {"changes": item.get("changes"),
                                                                   "status": item.get("status")}}] \
                    if t == "item.completed" else []
            return [{"type": "agent.raw", "payload": {"text": _trunc(json.dumps(item), 2000)}}]
        if t in ("error", "turn.failed"):
            msg = obj.get("message") or (obj.get("error") or {}).get("message") or line
            return [{"type": "agent.error", "payload": {"text": _trunc(str(msg), 2000)}}]
        if t == "turn.completed":
            return [{"type": "agent.usage", "payload": {"usage": obj.get("usage")}}]
        if t in ("thread.started", "turn.started"):
            return [{"type": "agent.system", "payload": {"subtype": t, "thread_id": obj.get("thread_id")}}]
        return [{"type": "agent.raw", "payload": {"text": _trunc(line, 2000)}}]
