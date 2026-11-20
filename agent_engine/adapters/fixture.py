"""Deterministic *test fixture* adapter (only when CODE_STUDIO_ENGINE_TEST_ADAPTER=1).

It is not an AI agent and is labelled as such in /agents. It runs a real
child process (Python) through the same subprocess/streaming/cancel/timeout
path as the CLI adapters, and interprets directives in the objective:

    WRITE <relpath> <<< <content, \\n for newline>
    APPEND <relpath> <<< <content>
    DELETE <relpath>
    SLEEP <seconds>
    FAIL <message>
    SAY <message>
    REPLY <final reply, \\n for newline>   (default: "fixture run complete")

Like the real CLIs it reports a session id (``system`` record) and, when
resumed, says which session it continued, so follow-ups can be tested.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .base import BaseAdapter, ProbeResult, _trunc, parse_json_line

SCRIPT = r'''
import json, os, sys, time, uuid
def emit(t, **p):
    sys.stdout.write(json.dumps({"type": t, **p}) + "\n"); sys.stdout.flush()
root = os.getcwd()
def safe(rel):
    full = os.path.realpath(os.path.join(root, rel))
    if not (full == root or full.startswith(root + os.sep)):
        raise SystemExit("refusing to write outside workspace: " + rel)
    return full
prompt = sys.stdin.read()
reply = "fixture run complete"
resumed = sys.argv[1] if len(sys.argv) > 1 else None
emit("system", session_id=resumed or uuid.uuid4().hex)
if resumed:
    emit("message", text="resumed session " + resumed)
emit("message", text="fixture adapter received %d chars" % len(prompt))
for line in prompt.splitlines():
    line = line.strip()
    if line.startswith(("WRITE ", "APPEND ")):
        cmd, rest = line.split(" ", 1)
        rel, _, content = rest.partition(" <<< ")
        full = safe(rel.strip())
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a" if cmd == "APPEND" else "w", encoding="utf-8", newline="") as fh:
            fh.write(content.replace("\\n", "\n"))
        emit("tool", tool="write", path=rel.strip())
    elif line.startswith("DELETE "):
        full = safe(line[7:].strip())
        if os.path.exists(full): os.remove(full)
        emit("tool", tool="delete", path=line[7:].strip())
    elif line.startswith("SLEEP "):
        secs = float(line[6:])
        end = time.time() + secs
        while time.time() < end:
            emit("message", text="working..."); time.sleep(0.2)
    elif line.startswith("FAIL "):
        emit("error", text=line[5:]); sys.exit(3)
    elif line.startswith("SAY "):
        emit("message", text=line[4:])
    elif line.startswith("REPLY "):
        reply = line[6:].replace("\\n", "\n")
emit("result", text=reply, num_turns=1, duration_ms=5)
'''


class FixtureAdapter(BaseAdapter):
    identifier = "fixture"
    display_name = "Test fixture (deterministic, not an AI agent)"
    supports_resume = True

    def _probe(self) -> ProbeResult:
        return ProbeResult(self.identifier, self.display_name, available=True, version="1",
                           executable=sys.executable, auth_state="not_required",
                           capabilities={"headless": True, "streaming": True, "staged_edits": True, "cancel": True,
                                         "test_only": True, "resume": True},
                           notes=["Enabled by CODE_STUDIO_ENGINE_TEST_ADAPTER=1 for automated verification"])

    def build_command(self, prompt: str, workspace: Path, policy: dict):
        argv = [sys.executable, "-I", "-c", SCRIPT]
        if policy.get("resume_session"):
            argv.append(str(policy["resume_session"]))
        return argv, prompt

    def parse_line(self, line: str) -> list[dict]:
        obj = parse_json_line(line) or {"type": "message", "text": line}
        t = obj.get("type")
        if t == "system":
            return [{"type": "agent.system", "payload": {"subtype": "init", "session_id": obj.get("session_id")}}]
        if t == "tool":
            return [{"type": "agent.tool_use", "payload": {"tool": obj.get("tool"), "input": obj.get("path")}}]
        if t == "result":
            return [{"type": "agent.result", "payload": {"text": obj.get("text", ""), "num_turns": obj.get("num_turns"),
                                                         "duration_ms": obj.get("duration_ms")}}]
        if t == "error":
            return [{"type": "agent.error", "payload": {"text": obj.get("text", "")}}]
        return [{"type": "agent.message", "payload": {"text": _trunc(str(obj.get("text", "")))}}]
