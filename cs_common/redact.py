"""Secret redaction and environment sanitisation.

Used on: terminal output, agent event payloads, audit details, task logs.
"""
from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Iterable, Mapping

REDACTED = "[REDACTED]"

# Environment variable *names* that are treated as secrets.
SECRET_ENV_NAME = re.compile(
    r"(^|_)(SECRET|SECRETS|TOKEN|TOKENS|PASSWORD|PASSWD|PASS|PWD_HASH|APIKEY|API_KEY|KEY|PRIVATE_KEY|"
    r"CREDENTIAL|CREDENTIALS|AUTH|SESSION|COOKIE|SAS|CONNECTION_STRING|CONNSTR|DSN)(_|$)",
    re.IGNORECASE,
)
# Names that match the pattern above but are not secrets.
SAFE_ENV_NAMES = {"SESSIONNAME", "KEYBOARD_LAYOUT", "PASSWORD_STORE_DIR"}

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), REDACTED),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-]{16,}=*"), r"\1" + REDACTED),
    (re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + REDACTED + r"\2"),
    (re.compile(
        r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|client[_-]?secret|access[_-]?key)\w*)"
        r"(\s*[:=]\s*)([\"']?)[^\s\"',;]{6,}\3"), r"\1\2\3" + REDACTED + r"\3"),
]


def is_secret_env_name(name: str) -> bool:
    return name.upper() not in SAFE_ENV_NAMES and bool(SECRET_ENV_NAME.search(name))


def env_secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    vals = {v for k, v in env.items() if is_secret_env_name(k) and v and len(v) >= 8}
    return sorted(vals, key=len, reverse=True)


class Redactor:
    def __init__(self, extra_values: Iterable[str] = (), include_env: bool = True):
        values = set(v for v in extra_values if v and len(v) >= 6)
        if include_env:
            values.update(env_secret_values())
        self._values = sorted(values, key=len, reverse=True)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text

    def redact_obj(self, obj: Any, _depth: int = 0) -> Any:
        if _depth > 20:
            return obj
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, Mapping):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and is_secret_env_name(k) and isinstance(v, str) and v:
                    out[k] = REDACTED
                else:
                    out[k] = self.redact_obj(v, _depth + 1)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v, _depth + 1) for v in obj]
        return obj


_default: Redactor | None = None


def default_redactor() -> Redactor:
    global _default
    if _default is None:
        _default = Redactor()
    return _default


def redact(text: str) -> str:
    return default_redactor().redact(text)


def redact_obj(obj: Any) -> Any:
    return default_redactor().redact_obj(obj)


# Database connection settings: never given to agents or check commands, so nothing
# an agent runs (manage.py, tests, migrations) can reach a real database through them.
DATABASE_ENV_NAME = re.compile(
    r"^(DATABASES?(_[A-Z0-9]+)*|DB(_[A-Z0-9]+)+|PG[A-Z]+|MYSQL(_[A-Z0-9]+)*|MARIADB(_[A-Z0-9]+)*|"
    r"MSSQL(_[A-Z0-9]+)*|SQLSERVER(_[A-Z0-9]+)*|ORACLE(_[A-Z0-9]+)*|MONGO(DB)?(_[A-Z0-9]+)*|"
    r"REDIS(_[A-Z0-9]+)*|[A-Z0-9_]*_DATABASE_URL)$",
    re.IGNORECASE,
)
# a URL carrying a password ("postgres://user:pass@host/db"), whatever the variable is called
CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)


def is_database_env(name: str, value: str = "") -> bool:
    return bool(DATABASE_ENV_NAME.match(name)) or bool(CREDENTIAL_URL.search(value or ""))


def agent_env(env: Mapping[str, str] | None = None, keep: Iterable[str] = (),
              drop: Iterable[str] = ()) -> dict[str, str]:
    """Environment for agent CLIs and check commands: ``sanitize_env`` minus database settings."""
    keep_u = {k.upper() for k in keep}
    return {k: v for k, v in sanitize_env(env, keep, drop).items()
            if k.upper() in keep_u or not is_database_env(k, v)}


def sanitize_env(env: Mapping[str, str] | None = None, keep: Iterable[str] = (),
                 drop: Iterable[str] = ()) -> dict[str, str]:
    """Copy of ``env`` without secret-looking variables (``keep`` names survive)."""
    env = os.environ if env is None else env
    keep_u = {k.upper() for k in keep}
    drop_u = {k.upper() for k in drop}
    out = {}
    for k, v in env.items():
        ku = k.upper()
        if ku in drop_u:
            continue
        if ku in keep_u or not is_secret_env_name(k):
            out[k] = v
    return out


# Files that are never copied into agent workspaces, never applied from agent
# change sets and never shown in agent context.
SENSITIVE_FILE_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", "_netrc", ".git-credentials",
    ".pypirc", ".npmrc", ".yarnrc", ".dockercfg", "credentials", "credentials.json",
    "*.tfstate", "*.tfstate.*", "secrets.json", "secrets.yaml", "secrets.yml",
]
SENSITIVE_FILE_ALLOW = {".env.example", ".env.sample", ".env.template", ".env.dist"}
SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".azure", ".kube"}


def is_sensitive_path(relpath: str) -> bool:
    parts = [p for p in relpath.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    if any(p.lower() in SENSITIVE_DIRS for p in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name in SENSITIVE_FILE_ALLOW:
        return False
    if name in SENSITIVE_DIRS:
        return True
    return any(fnmatch.fnmatch(name, g) for g in SENSITIVE_FILE_GLOBS)
