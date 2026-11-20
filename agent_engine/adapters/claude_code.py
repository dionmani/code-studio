"""Claude Code CLI adapter (verified against claude 2.1.x on Windows).

Headless interface: ``claude -p --output-format stream-json --verbose``,
prompt on stdin. Permission modes:
* staged edits  -> ``--permission-mode acceptEdits`` (file edits allowed inside
  the staging workspace; shell commands are not auto-approved in print mode)
* read-only     -> ``--permission-mode plan`` (no edits)
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import MAX_RESULT_TEXT, BaseAdapter, ProbeResult, _trunc, parse_json_line


class ClaudeCodeAdapter(BaseAdapter):
    identifier = "claude-code"
    display_name = "Claude Code"
    executable_names = ("claude",)
    supports_resume = True
    env_keep = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

    def _probe(self) -> ProbeResult:
        exe = self.find_executable()
        res = ProbeResult(self.identifier, self.display_name, available=False, executable=exe,
                          capabilities={"headless": True, "streaming": True, "staged_edits": True,
                                        "read_only": True, "cancel": True, "model_select": True,
                                        "resume": True})
        if not exe:
            res.notes.append("'claude' executable not found on PATH")
            res.capabilities = {}
            return res
        code, out = self._run_quick([exe, "--version"])
        if code != 0:
            res.notes.append(f"'claude --version' failed: {out.strip()[:200]}")
            return res
        res.version = out.strip().splitlines()[0] if out.strip() else None
        code, out = self._run_quick([exe, "auth", "status"])
        if code == 0:
            try:
                status = json.loads(out[out.index("{"):])
                res.auth_state = "logged_in" if status.get("loggedIn") else "logged_out"
                if status.get("authMethod"):
                    res.notes.append(f"auth method: {status['authMethod']}")
            except ValueError:
                res.auth_state = "unknown"
        res.available = res.auth_state != "logged_out"
        if res.auth_state == "logged_out":
            res.notes.append("Claude Code is installed but not logged in (run 'claude' to log in)")
        return res

    def build_command(self, prompt: str, workspace: Path, policy: dict):
        exe = self.find_executable()
        mode = "plan" if policy.get("read_only") else "acceptEdits"
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", mode]
        if policy.get("model"):
            argv += ["--model", str(policy["model"])]
        if policy.get("no_tools"):
            # plain text reply: no built-in tools, MCP servers, skills or coding-agent system
            # prompt (they would add tens of thousands of input tokens to a one-line answer)
            argv += ["--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                     "--system-prompt", "You are a concise assistant. Follow the user's instructions exactly."]
        if policy.get("max_turns"):
            argv += ["--max-turns", str(int(policy["max_turns"]))]
        if policy.get("resume_session"):
            # continues the conversation stored for this cwd (the task keeps its staging copy path)
            argv += ["--resume", str(policy["resume_session"])]
        return argv, prompt

    def parse_line(self, line: str) -> list[dict]:
        obj = parse_json_line(line)
        if obj is None:
            return [{"type": "agent.output", "payload": {"text": _trunc(line)}}]
        t = obj.get("type")
        events = []
        if t == "system":
            events.append({"type": "agent.system", "payload": {"subtype": obj.get("subtype"),
                                                                "model": obj.get("model"),
                                                                "session_id": obj.get("session_id")}})
        elif t == "assistant":
            for block in (obj.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    events.append({"type": "agent.message", "payload": {"text": _trunc(block["text"])}})
                elif block.get("type") == "tool_use":
                    events.append({"type": "agent.tool_use", "payload": {
                        "tool": block.get("name"), "input": _trunc(json.dumps(block.get("input"), default=str), 2000)}})
                elif block.get("type") == "thinking":
                    events.append({"type": "agent.thinking", "payload": {"text": _trunc(block.get("thinking", ""), 2000)}})
        elif t == "user":
            for block in (obj.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    events.append({"type": "agent.tool_result", "payload": {
                        "is_error": bool(block.get("is_error")), "text": _trunc(str(content or ""), 2000)}})
        elif t == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            events.append({"type": "agent.system", "payload": {"subtype": "rate_limit", "status": info.get("status"),
                                                                "limit_type": info.get("rateLimitType")}})
        elif t == "result":
            events.append({"type": "agent.result", "payload": {
                "text": _trunc(str(obj.get("result") or ""), MAX_RESULT_TEXT), "is_error": bool(obj.get("is_error")),
                "subtype": obj.get("subtype"), "num_turns": obj.get("num_turns"),
                "duration_ms": obj.get("duration_ms"), "cost_usd": obj.get("total_cost_usd")}})
        else:
            events.append({"type": "agent.raw", "payload": {"text": _trunc(line, 2000)}})
        return events
