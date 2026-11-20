"""Resolution of the internal application data directory.

The data directory holds the SQLite DB, recovery snapshots, change-set
artifacts, engine workspaces and the shared service token. It must live
outside any project tree; by default it is ``%LOCALAPPDATA%\\CodeStudio``.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def default_data_dir() -> Path:
    env = os.environ.get("CODE_STUDIO_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return (Path(base) / "CodeStudio").resolve()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_or_create_secret(path: Path, nbytes: int = 32) -> str:
    """Read a secret from ``path`` or create it (owner-only where supported)."""
    ensure_dir(path.parent)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(nbytes)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value


def engine_token(data_dir: Path | None = None) -> str:
    """Shared IDE<->engine service token (env var wins over the token file)."""
    env = os.environ.get("CODE_STUDIO_ENGINE_TOKEN")
    if env:
        return env
    return read_or_create_secret((data_dir or default_data_dir()) / "engine_token")
