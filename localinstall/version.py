"""Which build of Code Studio is running.

Two parts:

* ``version`` -- the human-readable release in the top-level ``VERSION`` file.
* ``build``   -- a fingerprint of the source files themselves, so two copies
  are compared by what they actually contain. Nobody has to remember to bump
  ``VERSION`` for a local copy to notice that the server changed.

The server computes the fingerprint from its own source. The install package
writes it into ``BUILD_INFO.json``, so a local copy reports exactly the build it
was installed from without re-hashing.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from django.conf import settings

from cs_common.redact import is_sensitive_path

BUILD_INFO_NAME = "BUILD_INFO.json"

# Never part of the source: environments, caches, build output, local data.
# Keep in step with .dockerignore so a dev checkout and the Docker image agree.
EXCLUDED_DIRS = {"__pycache__", "staticfiles", ".git", "node_modules", ".pytest_cache", ".pytest_tmp",
                 ".e2e", ".idea", ".vscode", "venv", "env"}
EXCLUDED_DIR_PREFIXES = (".venv",)
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".sqlite3", ".sqlite3-journal", ".log")
EXCLUDED_NAMES = {BUILD_INFO_NAME, ".DS_Store", "Thumbs.db"}


def base_dir() -> Path:
    return Path(settings.BASE_DIR)


def iter_source_files(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(posix relative path, absolute path)`` for every source file, sorted."""
    found: list[tuple[str, Path]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            name = entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if name in EXCLUDED_DIRS or name.startswith(EXCLUDED_DIR_PREFIXES):
                    continue
                stack.append(entry)
                continue
            if name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES):
                continue
            rel = entry.relative_to(root).as_posix()
            if is_sensitive_path(rel):  # .env, keys, credentials ...
                continue
            found.append((rel, entry))
    found.sort(key=lambda item: item[0])
    yield from found


def fingerprint(root: Path) -> str:
    """Short hash of every source file's path and content (line endings normalised,
    so a Windows checkout and the Linux image of the same code match)."""
    digest = hashlib.sha256()
    for rel, path in iter_source_files(root):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:12]


def read_version(root: Path) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


@lru_cache(maxsize=1)
def build_info() -> dict:
    """``{"version", "build", "built_at"}`` for the running copy (cached for the process)."""
    root = base_dir()
    stamped = root / BUILD_INFO_NAME
    if stamped.is_file():
        try:
            data = json.loads(stamped.read_text(encoding="utf-8"))
            if data.get("build"):
                return {"version": str(data.get("version", "")), "build": str(data["build"]),
                        "built_at": str(data.get("built_at", ""))}
        except (OSError, ValueError):
            pass
    return {"version": read_version(root), "build": fingerprint(root), "built_at": ""}


def label(info: dict | None = None) -> str:
    info = info or build_info()
    return f"{info['version']} ({info['build']})"
