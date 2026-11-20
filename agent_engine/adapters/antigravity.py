"""Google Antigravity CLI (``agy``) adapter — opt-in.

Verified on this machine (2026-11-19, agy 1.5.12):
* Headless mode: ``agy --print=<prompt> --output-format stream-json
  --mode plan|accept-edits`` emits NDJSON records ``{"event": "...", ...}``:
  ``init`` (top-level ``conversation_id``), ``step_update`` (nested
  ``step_update.conversation_id``, ``text_delta``) and the final
  ``{"event": "result", "result": {"conversation_id", "status", "response", "num_turns", ...}}``.
* ``--conversation=<conversation_id>`` resumes that conversation headlessly
  (the agent remembered its earlier reply; ``num_turns`` went 1 -> 2).
* ``--mode plan`` is ignored when ``--disable-slash-commands`` is also given
  (agy warns "--mode plan has no effect while slash command expansion is
  disabled"). Without that flag plan mode held: asked to create a file, the
  agent only wrote a plan. So read-only runs omit ``--disable-slash-commands``.
* Login: agy keeps its Google OAuth tokens in the OS keyring (service
  ``gemini``, account ``antigravity``). On Windows that is a Credential
  Manager entry ``gemini:antigravity``; the probe checks that it *exists*
  (``cmdkey /list:<target>``, which never prints the secret). Without a login
  every headless call starts an interactive OAuth flow and blocks ~60 s.

The adapter is still off until an admin turns it on in Settings > Agent team
(the old ``CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY=1`` only seeds that switch).
When it is on, a missing login makes it unavailable instead of hanging. Where
the keyring cannot be checked (Linux/Docker), login is assumed as before. The
probe never runs a prompt, so it cannot hang on the OAuth flow.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from cs_common.agent_policy import PolicyError, clean_allowed_commands

from .base import MAX_RESULT_TEXT, AdapterUnavailable, BaseAdapter, ProbeResult, _trunc, parse_json_line

ENABLE_ENV = "CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY"  # legacy: only seeds the switch once (see settings_store)
KEYRING_TARGET = "gemini:antigravity"  # Windows Credential Manager entry holding agy's OAuth tokens


class AntigravityAdapter(BaseAdapter):
    identifier = "antigravity"
    display_name = "Google Antigravity"
    executable_names = ("agy", "antigravity")
    env_keep = ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT")
    toggleable = True  # turned on/off at runtime by an admin (engine settings.json)
    supports_resume = True  # --conversation=<conversation_id> (verified, see module docstring)

    def _probe(self) -> ProbeResult:
        exe = self.find_executable()
        res = ProbeResult(self.identifier, self.display_name, available=False, executable=exe,
                          capabilities={"headless": bool(exe), "streaming": bool(exe), "staged_edits": bool(exe),
                                        "read_only": bool(exe), "cancel": bool(exe), "resume": bool(exe),
                                        "verified_events": ["init", "step_update", "result"]})
        if not exe:
            res.notes.append("No Antigravity executable ('agy') found on PATH")
            res.capabilities = {}
            return res
        code, out = self._run_quick([exe, "--version"])
        res.version = out.strip().splitlines()[0] if code == 0 and out.strip() else None
        login = self.keyring_login()
        if login is True:
            res.auth_state = "logged_in"
        elif login is False:
            res.auth_state = "logged_out"
        if not self.is_enabled():
            if login is None:
                res.auth_state = "unverified"
            if login:
                res.notes.append("Logged in. Turned off: an admin turns Antigravity on in Settings > Agent team.")
            else:
                res.notes.append("Turned off: run 'agy' once to log in (Docker: docker compose exec -it engine agy), "
                                 "then an admin turns Antigravity on in Settings > Agent team.")
            return res
        if login is False:
            res.notes.append("Antigravity is installed but not logged in: run 'agy' once to log in")
            return res
        if login is None:
            res.auth_state = "assumed_logged_in"
            res.notes.append("Turned on by an admin; login cannot be checked here (no Windows Credential Manager)")
        res.available = True
        return res

    def keyring_login(self) -> bool | None:
        """Whether agy's saved login exists: True/False on Windows, None where it cannot be checked.
        Only the entry's presence is checked (cmdkey never prints the secret); an expired login
        still counts as present."""
        if os.name != "nt":
            return None
        cmdkey = shutil.which("cmdkey")
        if not cmdkey:
            return None
        code, out = self._run_quick([cmdkey, f"/list:{KEYRING_TARGET}"], timeout=10)
        if code != 0:
            return None
        return any(line.strip().lower() == f"target: {KEYRING_TARGET}" for line in out.splitlines())

    def build_command(self, prompt: str, workspace: Path, policy: dict):
        exe = self.find_executable()
        if not exe:
            raise AdapterUnavailable("agy not found")
        # agy has no stdin prompt mode for plain text; the prompt is passed as a
        # single argv element to a native .exe (no cmd.exe parsing involved).
        read_only = bool(policy.get("read_only"))
        argv = [exe, "--output-format", "stream-json", "--mode", "plan" if read_only else "accept-edits"]
        if not read_only:
            # agy ignores --mode plan when slash commands are disabled, so read-only runs keep
            # expansion on. Prompts never start with "/": they open with Code Studio's own text.
            argv.append("--disable-slash-commands")
        argv.append(f"--print-timeout={int(policy.get('timeout_seconds') or 900)}s")
        if policy.get("model"):
            argv.append(f"--model={policy['model']}")
        if policy.get("resume_session"):
            argv.append(f"--conversation={policy['resume_session']}")
        allowed = self.allowed_commands(policy)
        if allowed:
            # headless agy auto-denies shell commands without an allow-rule; these are the
            # profile's opt-in prefixes (validated: no shells/interpreters/metacharacters).
            # Session-only: never written to the user's settings.json.
            argv.append("--settings=" + json.dumps({"permissions": {"allow": [f"command({c})" for c in allowed]}}))
        argv.append(f"--print={prompt}")
        return argv, ""

    def max_prompt_chars(self) -> int | None:
        # the prompt is one argv element (--print=...): Windows caps a whole command line at
        # 32767 characters; Linux caps one argument at 128 KiB
        return 26_000 if os.name == "nt" else 120_000

    @staticmethod
    def allowed_commands(policy: dict) -> list[str]:
        if policy.get("read_only"):
            return []
        try:
            return clean_allowed_commands(policy.get("allowed_commands"))
        except PolicyError:
            return []

    def prompt_notes(self, policy: dict) -> str:
        allowed = self.allowed_commands(policy)
        note = ("Shell commands need approval that cannot be given in this non-interactive run, so unlisted "
                "commands are refused. Use your built-in tools to list, view and search files instead.")
        if allowed:
            note += " Shell commands you may run: " + ", ".join(allowed) + " (as plain commands, no ; chaining)."
        return note

    def parse_line(self, line: str) -> list[dict]:
        obj = parse_json_line(line)
        if obj is None:
            return [{"type": "agent.output", "payload": {"text": _trunc(line, 2000)}}]
        events = []
        conv = conversation_id(obj)
        if conv:  # reported so a follow-up can pass --conversation=<id>
            events.append({"type": "agent.system", "payload": {"subtype": obj.get("event"), "conversation_id": conv}})
        return events + self._parse_record(obj, line)

    def _parse_record(self, obj: dict, line: str) -> list[dict]:
        if obj.get("event") == "result":
            r = obj.get("result") or {}
            is_error = r.get("status") not in (None, "OK", "SUCCESS", "success", "ok")
            return [{"type": "agent.result", "payload": {
                "text": _trunc(str(r.get("response") or r.get("error") or ""), MAX_RESULT_TEXT), "is_error": is_error,
                "status": r.get("status"), "num_turns": r.get("num_turns"),
                "duration_ms": int(float(r.get("duration_seconds") or 0) * 1000)}}]
        return [{"type": "agent.raw", "payload": {"event": obj.get("event"), "text": _trunc(line, 2000)}}]


CONVERSATION_KEYS = ("conversation_id", "conversationId", "session_id", "sessionId")


def conversation_id(obj: dict) -> str | None:
    """The conversation id in an agy record: top-level on ``init``, inside
    ``result`` on the final record (the other keys are tolerated for older/newer CLIs)."""
    for scope in (obj, obj.get("result") if isinstance(obj.get("result"), dict) else {}):
        for key in CONVERSATION_KEYS:
            value = scope.get(key)
            if isinstance(value, str) and 0 < len(value) <= 200:
                return value
    return None
