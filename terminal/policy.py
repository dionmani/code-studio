"""Command policy for the scoped terminal (design gate 6).

The terminal is *line-oriented*: each submitted command runs as its own
child process (PowerShell on Windows, bash on Linux) with ``cwd`` inside the project root and
a sanitised environment. Before running, the command line is checked:

* ``cd`` is a server built-in whose target must stay inside the project root.
* Path-like tokens (absolute, UNC, ``~``, ``..``, profile/env-var paths) must
  resolve inside the project root.
* Privilege-escalation / system-modification commands and link creation
  (which could escape containment) are refused.

This is a policy guard that blocks accidental and casual escapes; it is not an
OS sandbox. For untrusted users run Code Studio in a container (see docs).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from projects.paths import canonical, is_within

# Start of a command: line start or after ; & | ( ` $( , optionally behind
# env assignments / common wrappers (env, nohup, nice, time, xargs, exec, command).
_CMD = (r"(?:^|[;&|(`]|\$\()\s*(?:\w+=\S*\s+)*"
        r"(?:(?:env|nohup|nice|time|xargs|exec|command|busybox|sudo)\s+(?:-\S+\s+)*(?:\w+=\S*\s+)*)*")

BLOCKED_PATTERNS = [
    (r"\brunas\b", "privilege escalation (runas)"),
    (r"-verb\s+runas", "privilege escalation (Start-Process -Verb RunAs)"),
    (r"\bsudo\b|\bgsudo\b", "privilege escalation (sudo)"),
    (r"\bshutdown(\.exe)?\b|\bstop-computer\b|\brestart-computer\b", "system power control"),
    (r"\bformat(\.com)?\s+[a-z]:", "disk formatting"),
    (r"\bdiskpart\b|\bbcdedit\b|\bvssadmin\b|\bwbadmin\b", "system administration tool"),
    (r"\breg(\.exe)?\s+(add|delete|import|load|restore|copy)\b", "registry modification"),
    (r"\b(set|new|remove)-itemproperty\b.*\bhk(lm|cu|cr|u)\b", "registry modification"),
    (r"\bset-executionpolicy\b", "execution policy change"),
    (r"\bnet(\.exe)?\s+(user|localgroup|share)\b", "account/share administration"),
    (r"\bsc(\.exe)?\s+(create|delete|config|stop)\b|\b(new|set|remove|stop)-service\b", "service administration"),
    (r"\bschtasks\b|\bregister-scheduledtask\b", "scheduled task creation"),
    (r"\bmklink\b", "link creation can escape the project root"),
    (r"-itemtype\s+[\"']?(symboliclink|junction|hardlink)", "link creation can escape the project root"),
    (r"\bcipher(\.exe)?\s+/w\b", "secure wipe"),
    (r"\b(takeown|icacls|cacls)\b", "ACL / ownership change"),
    (r"\bsubst\b|\bnet\s+use\b|\bnew-psdrive\b", "drive mapping can escape the project root"),
    (r"\bdocker(\.exe)?\b.*(-v|--volume|--mount)\b", "host bind mounts are not allowed"),
    (r"\\\\\.\\pipe\\docker_engine|/var/run/docker\.sock", "Docker socket access"),
    (r"\bset-location\b|\bpush-location\b|\bpushd\b|\bpopd\b|\bchdir\b", "use the built-in 'cd' instead"),
    (r"\b(get-content|cat|type|gc)\b.*\$env:", "reading environment-relative paths"),
    (r"\bget-childitem\s+env:|\bdir\s+env:|\bls\s+env:|\bgci\s+env:|^\s*set\s*$|^\s*env\s*$|\bprintenv\b"
     r"|\bexport\s+-p\b|^\s*export\s*$|\bdeclare\s+-[a-z]*x|\bcompgen\s+-e\b",
     "listing environment variables is disabled (secrets)"),
    # POSIX (Docker/Linux deployment). Anchored to command position so that
    # words inside arguments (e.g. a commit message mentioning "mount") pass.
    (_CMD + r"(su|doas|pkexec)\b", "privilege escalation"),
    (_CMD + r"(chroot|nsenter|unshare|mount|umount|losetup|pivot_root)\b", "namespace / mount manipulation"),
    (_CMD + r"ln\b", "link creation can escape the project root"),
    (_CMD + r"(chown|chgrp|setfacl)\b", "ownership / ACL change"),
    (_CMD + r"(systemctl|service|crontab|at)\b", "service / scheduled task administration"),
    (_CMD + r"(mkfs(\.\w+)?|fdisk|parted|wipefs|dd)\b", "disk-level tool"),
]
_BLOCKED = [(re.compile(p, re.IGNORECASE), why) for p, why in BLOCKED_PATTERNS]

# Environment variable references that expand to locations outside the project.
_ENV_PATH_REF = re.compile(
    r"(\$env:(userprofile|homepath|homedrive|appdata|localappdata|programdata|systemroot|windir|temp|tmp|"
    r"public|onedrive|allusersprofile|programfiles)|%(userprofile|homepath|homedrive|appdata|localappdata|"
    r"programdata|systemroot|windir|temp|tmp|public|onedrive|allusersprofile|programfiles)%|\$home\b|\$pshome\b|"
    r"\$profile\b|\$\{?(home|tmpdir|oldpwd|xdg_\w+)\b\}?)", re.IGNORECASE)
_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]?|\\\\|//|/(?!/)|~)")
_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s;|&<>()]+')


class PolicyDecision:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self):
        return self.allowed


def _strip(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def _path_candidates(token: str) -> list[str]:
    tok = _strip(token)
    # flags such as -Path:C:\x or --out=C:\x
    for sep in ("=", ":"):
        if tok.startswith("-") and sep in tok:
            tail = tok.split(sep, 1)[1]
            if tail:
                return [tail]
    return [tok]


def _looks_like_path(tok: str) -> bool:
    if not tok or tok.startswith("-") and not _ABS.match(tok):
        return False
    # Windows-style switches such as /c /s /b /i. Only on Windows: on POSIX
    # "/etc" or "/data" are real absolute paths and must be containment-checked.
    if os.name == "nt" and re.match(r"^/[A-Za-z0-9?:+\-]{1,12}$", tok):
        return False
    return bool(_ABS.match(tok)) or ".." in tok.replace("\\", "/").split("/") or "\\" in tok or "/" in tok


def check_path(raw: str, cwd: Path, root: Path) -> PolicyDecision:
    if raw.startswith("~"):
        return PolicyDecision(False, "home-relative paths are outside the project")
    if re.match(r"^[A-Za-z]+://", raw):  # URLs are fine
        return PolicyDecision(True)
    candidate = Path(raw) if os.path.isabs(raw) or re.match(r"^[A-Za-z]:", raw) else cwd / raw
    if re.match(r"^[A-Za-z]:(?![\\/])", raw):
        return PolicyDecision(False, "drive-relative paths are not allowed")
    if raw.startswith("/") and not raw.startswith("//") and os.name == "nt":
        candidate = Path(cwd.anchor) / raw.lstrip("/")
    resolved = canonical(candidate)
    if not is_within(resolved, root):
        return PolicyDecision(False, f"path '{raw}' is outside the project root")
    return PolicyDecision(True)


def check_command(command: str, cwd: Path, root: Path) -> PolicyDecision:
    if not command.strip():
        return PolicyDecision(False, "empty command")
    if len(command) > 8000:
        return PolicyDecision(False, "command too long")
    if "\x00" in command:
        return PolicyDecision(False, "invalid character")
    for pattern, why in _BLOCKED:
        if pattern.search(command):
            return PolicyDecision(False, f"blocked: {why}")
    if _ENV_PATH_REF.search(command):
        return PolicyDecision(False, "blocked: environment-variable paths point outside the project root")
    for token in _TOKEN.findall(command):
        for cand in _path_candidates(token):
            if _looks_like_path(cand):
                decision = check_path(cand, cwd, root)
                if not decision:
                    return PolicyDecision(False, f"blocked: {decision.reason}")
    return PolicyDecision(True)


def resolve_cd(arg: str, cwd: Path, root: Path) -> tuple[Path | None, str]:
    arg = _strip(arg.strip()) or "."
    if arg in ("~",):
        return root, ""
    if arg.startswith("~"):
        return None, "home-relative paths are outside the project"
    target = Path(arg) if os.path.isabs(arg) or re.match(r"^[A-Za-z]:[\\/]", arg) else cwd / arg
    resolved = canonical(target)
    if not is_within(resolved, root):
        return None, "cd target is outside the project root"
    if not resolved.is_dir():
        return None, "no such directory"
    return resolved, ""
