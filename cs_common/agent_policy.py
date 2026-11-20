"""Validation of the per-profile "allowed commands" list (shared by IDE and engine).

Antigravity runs headless and auto-denies every shell command that has no
allow-rule (``command(<prefix>)``). Verified with agy 1.5.10:

* rules match by prefix, and each part of ``a && b`` is checked separately;
  ``a; b`` with an unlisted ``b`` is denied;
* **output redirection is not checked**: with ``command(cat)`` allowed,
  ``cat a > /tmp/x`` ran and wrote outside the workspace.

So an allowed command can read and write anything the engine user can. The list is
opt-in (empty by default), never applies to read-only profiles, accepts plain
command prefixes only, and refuses shells, interpreters run bare, and network or
privilege tools that would amount to "allow everything".
"""
from __future__ import annotations

import re

MAX_COMMANDS = 20
MAX_LEN = 100
_SAFE = re.compile(r"^[A-Za-z0-9./][A-Za-z0-9 ._/:=@+,-]*$")
# never allowed as the first word: they run arbitrary code or reach the network
BLOCKED = {"sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "pwsh", "powershell", "cmd", "env", "sudo",
           "su", "doas", "eval", "exec", "xargs", "nohup", "timeout", "nice", "curl", "wget", "nc", "ncat", "netcat",
           "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "socat", "chmod", "chown", "rm", "dd", "mv", "cp", "ln",
           "find", "awk", "gawk", "sed", "perl", "ruby", "osascript",
           "git"}  # git: there is no .git in the staging copy, and -c options can run code
# interpreters/runners: allowed only with a specific subcommand or script ("npm test", "python -m pytest")
NEEDS_SUBCOMMAND = {"python", "python3", "py", "node", "deno", "bun", "npx", "npm", "pnpm", "yarn", "php", "java",
                    "go", "cargo", "dotnet", "make", "uv", "pip", "pip3"}


class PolicyError(ValueError):
    pass


def clean_allowed_commands(raw) -> list[str]:
    """Return a de-duplicated list of command prefixes, or raise PolicyError."""
    if raw in (None, "", []):
        return []
    items = raw.splitlines() if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise PolicyError("Allowed commands must be a list")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise PolicyError("Allowed commands must be text")
        cmd = " ".join(item.split())
        if not cmd:
            continue
        if len(cmd) > MAX_LEN:
            raise PolicyError(f"Allowed command too long (max {MAX_LEN} characters): {cmd[:40]}…")
        if not _SAFE.match(cmd):
            raise PolicyError(f"\"{cmd}\": use a plain command prefix such as \"npm test\" "
                              "(no *, quotes, pipes, redirects, ; & $ or brackets)")
        first = cmd.split(" ", 1)[0].lower()
        base = first.rsplit("/", 1)[-1]
        if base in BLOCKED:
            raise PolicyError(f"\"{cmd}\": '{base}' can't be allowed (it could run or reach anything)")
        if base in NEEDS_SUBCOMMAND and " " not in cmd:
            raise PolicyError(f"\"{cmd}\": name the subcommand too, e.g. \"{base} test\" or \"python -m pytest\"")
        if cmd not in out:
            out.append(cmd)
    if len(out) > MAX_COMMANDS:
        raise PolicyError(f"At most {MAX_COMMANDS} allowed commands")
    return out


# ---- per-project checks: verify commands, forced environment, exclusions ----------

MAX_ENV = 20
MAX_EXCLUDE = 50
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# variables a project may not force: they change which programs or libraries run
BLOCKED_ENV = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP",
               "PYTHONINSPECT", "NODE_OPTIONS", "NODE_PATH", "VIRTUAL_ENV", "HOME", "USERPROFILE", "APPDATA",
               "LOCALAPPDATA", "TEMP", "TMP", "CODE_STUDIO_WORKSPACE"}
_BLOCKED_ENV_PREFIXES = ("LD_", "DYLD_", "CODE_STUDIO_")


def clean_check_env(raw) -> dict[str, str]:
    """``{"NAME": "value"}`` (or "NAME=value" lines) forced on agents and check commands."""
    from .redact import is_database_env

    if raw in (None, "", {}, []):
        return {}
    if isinstance(raw, str):
        pairs = []
        for line in raw.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise PolicyError(f"\"{line.strip()[:40]}\": write NAME=value")
            name, _, value = line.partition("=")
            pairs.append((name.strip(), value.strip()))
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        raise PolicyError("Environment must be NAME=value lines")
    out: dict[str, str] = {}
    for name, value in pairs:
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise PolicyError(f"\"{str(name)[:40]}\" is not a valid variable name")
        value = "" if value is None else str(value)
        up = name.upper()
        if up in BLOCKED_ENV or up.startswith(_BLOCKED_ENV_PREFIXES):
            raise PolicyError(f"{name} can't be set here (it changes which programs run)")
        if is_database_env(name, value):
            raise PolicyError(f"{name}: database settings are never given to agents; point your test settings "
                              "at a local test database instead")
        if len(value) > 500 or "\n" in value or "\r" in value:
            raise PolicyError(f"{name}: value must be one line of at most 500 characters")
        out[name] = value
    if len(out) > MAX_ENV:
        raise PolicyError(f"At most {MAX_ENV} environment variables")
    return out


def clean_excludes(raw) -> list[str]:
    """Glob patterns for project files never copied to agents ("*.csv", "data/prod_*")."""
    if raw in (None, "", []):
        return []
    items = raw.splitlines() if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise PolicyError("Exclusions must be a list of patterns")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise PolicyError("Exclusions must be text")
        pat = item.strip().replace("\\", "/").strip("/")
        if not pat or pat.startswith("#"):
            continue
        if len(pat) > 200 or ".." in pat.split("/") or ":" in pat:
            raise PolicyError(f"\"{pat[:40]}\": use a pattern inside the project, such as *.csv or data/prod_*")
        if pat not in out:
            out.append(pat)
    if len(out) > MAX_EXCLUDE:
        raise PolicyError(f"At most {MAX_EXCLUDE} exclusion patterns")
    return out


def is_excluded(relpath: str, patterns: list[str]) -> bool:
    """A pattern without "/" matches a file or folder name anywhere; with "/" it matches
    the path from the project root (or any folder beneath that path)."""
    import fnmatch

    rel = relpath.replace("\\", "/").strip("/")
    parts = rel.split("/")
    for pat in patterns:
        p = pat.lower()
        if "/" in pat:
            if fnmatch.fnmatch(rel.lower(), p) or any(fnmatch.fnmatch("/".join(parts[:i]).lower(), p)
                                                       for i in range(1, len(parts))):
                return True
        elif any(fnmatch.fnmatch(part.lower(), p) for part in parts):
            return True
    return False
