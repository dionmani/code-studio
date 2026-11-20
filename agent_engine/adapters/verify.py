"""Verify step: runs the project's check commands. Not an AI agent.

A lineup step like any other (profile with the "verify" agent), but instead of
asking a model it runs fixed commands -- e.g. ``python manage.py makemigrations
--check --dry-run`` and ``python -m pytest -q`` -- set per project in
Settings > Project checks. Every command runs even if an earlier one fails, and
the step's reply is a report of real exit codes and output, which later steps
(reviewer, fixer) receive like any other step's reply.

Safety, enforced by the engine rather than by the commands:

* commands pass the same validation as allowed commands
  (``cs_common.agent_policy.clean_allowed_commands``) and run without a shell;
* they run in a throwaway copy of the agents' working copy, so files they
  write (test databases, caches, migrations) never enter the change set;
* their environment has no secrets or database settings, has the project's
  forced variables (its test settings) and its Python environment first on PATH.

The step "succeeds" when the commands could be run; whether the checks passed is
reported in the reply and in ``checks_total`` / ``checks_failed``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .base import MAX_RESULT_TEXT, BaseAdapter, ProbeResult, _trunc, parse_json_line

RUNNER = r'''
import collections, json, os, shlex, shutil, subprocess, sys, time
def emit(t, **p):
    sys.stdout.write(json.dumps({"type": t, **p}) + "\n"); sys.stdout.flush()
spec = json.loads(sys.stdin.read())
MAX_LIVE = 2000   # output lines relayed live per command (the tail is always in the report)
TAIL = 120        # output lines per command kept in the report
results = []
for cmd in spec["commands"]:
    argv = shlex.split(cmd)
    exe = shutil.which(argv[0])
    emit("start", command=cmd)
    if not exe:
        results.append({"command": cmd, "exit_code": None, "seconds": 0, "tail": [],
                        "error": "command not found: %s (is the project's Python environment set?)" % argv[0]})
        emit("done", command=cmd, exit_code=None, error=results[-1]["error"])
        continue
    t0 = time.time()
    tail = collections.deque(maxlen=TAIL)
    live = 0
    proc = subprocess.Popen([exe] + argv[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        tail.append(line)
        live += 1
        if live <= MAX_LIVE:
            emit("output", text=line[:2000])
        elif live == MAX_LIVE + 1:
            emit("output", text="[further output not shown live; the end is in the report]")
    code = proc.wait()
    results.append({"command": cmd, "exit_code": code, "seconds": round(time.time() - t0, 1), "tail": list(tail),
                    "error": None})
    emit("done", command=cmd, exit_code=code)
failed = [r for r in results if r["exit_code"] != 0]
lines = ["## Checks: " + ("all %d passed" % len(results) if not failed
                          else "%d of %d failed" % (len(failed), len(results))), ""]
for r in results:
    ok = r["exit_code"] == 0
    how = r["error"] or "exit %s, %ss" % (r["exit_code"], r["seconds"])
    lines.append("### %s `%s` (%s)" % ("PASS" if ok else "FAIL", r["command"], how))
    if r["tail"] and not ok:
        lines += ["", "Last %d lines of output:" % len(r["tail"]), "```text"] + r["tail"] + ["```"]
    lines.append("")
emit("result", text="\n".join(lines), checks_total=len(results), checks_failed=len(failed))
'''


class VerifyAdapter(BaseAdapter):
    identifier = "verify"
    display_name = "Checks (runs the project's commands, not an AI)"
    runs_checks = True  # the task manager gives it the project's commands and a scratch copy

    def _probe(self) -> ProbeResult:
        return ProbeResult(self.identifier, self.display_name, available=True, version="1",
                           executable=sys.executable, auth_state="not_required",
                           capabilities={"headless": True, "streaming": True, "cancel": True, "checks": True},
                           notes=["Runs the check commands set in Settings > Project checks"])

    def build_command(self, prompt: str, workspace: Path, policy: dict):
        return [sys.executable, "-I", "-c", RUNNER], json.dumps({"commands": list(policy.get("commands") or [])})

    def parse_line(self, line: str) -> list[dict]:
        obj = parse_json_line(line) or {"type": "output", "text": line}
        t = obj.get("type")
        if t == "start":
            return [{"type": "agent.tool_use", "payload": {"tool": "check", "input": obj.get("command")}}]
        if t == "done":
            return [{"type": "agent.tool_result", "payload": {
                "tool": "check", "input": obj.get("command"), "exit_code": obj.get("exit_code"),
                "status": "passed" if obj.get("exit_code") == 0 else "failed", "text": obj.get("error") or ""}}]
        if t == "result":
            return [{"type": "agent.result", "payload": {
                "text": _trunc(str(obj.get("text") or ""), MAX_RESULT_TEXT), "checks_total": obj.get("checks_total"),
                "checks_failed": obj.get("checks_failed")}}]
        return [{"type": "agent.output", "payload": {"text": _trunc(str(obj.get("text", "")), 2000)}}]
