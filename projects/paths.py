"""Canonical path containment for every file, terminal, Git and agent operation.

Rules:
* Client paths are *relative* POSIX-or-Windows paths; absolute paths, drive
  letters, UNC paths, ``..`` segments, NUL/control characters, NTFS alternate
  data streams (``:``), reserved device names and trailing dots/spaces are
  rejected outright.
* The joined path is canonicalised with ``os.path.realpath`` (follows
  symlinks *and* NTFS junctions) and must remain inside the canonical project
  root, which itself must remain inside its canonical allowlisted root.
* ``.git`` internals are protected from the file API (hooks/config edits).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')
PROTECTED_DIRS = {".git"}


class PathSecurityError(Exception):
    """Raised for any path that is invalid or escapes its allowed scope."""

    status = 403


def canonical(path: str | os.PathLike) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def is_within(child: Path | str, parent: Path | str) -> bool:
    c = os.path.normcase(os.fspath(child)).rstrip("\\/")
    p = os.path.normcase(os.fspath(parent)).rstrip("\\/")
    if not p:
        return False
    return c == p or c.startswith(p + os.sep)


def split_relative(rel: str | None) -> list[str]:
    """Validate a client-supplied relative path and return its components."""
    if rel is None:
        return []
    if not isinstance(rel, str):
        raise PathSecurityError("Path must be a string")
    if len(rel) > 1024:
        raise PathSecurityError("Path too long")
    if "\x00" in rel:
        raise PathSecurityError("Invalid character in path")
    norm = rel.replace("\\", "/").strip()
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or norm.startswith("//"):
        raise PathSecurityError("Absolute paths are not allowed")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise PathSecurityError("Path traversal ('..') is not allowed")
        if ":" in part:
            raise PathSecurityError("':' is not allowed in path components")
        if _BAD_CHARS.search(part):
            raise PathSecurityError("Invalid character in path")
        if part != part.rstrip(" ."):
            raise PathSecurityError("Path components may not end with a dot or space")
        if part.split(".")[0].upper() in WIN_RESERVED:
            raise PathSecurityError(f"Reserved device name: {part}")
    return parts


def normalize_relative(rel: str | None) -> str:
    return "/".join(split_relative(rel))


def validate_root_path(path: str) -> Path:
    """Validate a directory proposed as an allowlisted root (admin action)."""
    if not path or not os.path.isabs(path):
        raise PathSecurityError("Root must be an absolute path")
    resolved = canonical(path)
    if not resolved.is_dir():
        raise PathSecurityError("Root directory does not exist")
    if Path(resolved.anchor) == resolved:
        raise PathSecurityError("A drive/filesystem root cannot be allowlisted")
    sysroot = os.environ.get("SystemRoot") or os.environ.get("windir")
    forbidden = [sysroot, os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramData")]
    for f in filter(None, forbidden):
        if is_within(resolved, canonical(f)):
            raise PathSecurityError("System directories cannot be allowlisted")
    if os.name != "nt":
        posix_system = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/root",
                        "/run", "/sbin", "/sys", "/usr", "/var", "/app")
        for f in posix_system:
            if is_within(resolved, Path(f)):
                raise PathSecurityError("System directories cannot be allowlisted")
    home = canonical(Path.home())
    if resolved == home:
        raise PathSecurityError("The user profile root cannot be allowlisted; choose a sub-folder")
    for sub in (".ssh", ".aws", ".gnupg", ".docker", ".kube", "AppData"):
        if is_within(resolved, home / sub):
            raise PathSecurityError("Credential/application data folders cannot be allowlisted")
    from django.conf import settings

    data_dir = canonical(settings.DATA_DIR)
    if is_within(resolved, data_dir) or is_within(data_dir, resolved):
        raise PathSecurityError("A root may not contain or be inside the Code Studio data directory")
    return resolved


def root_path(root) -> Path:
    """Canonical path of an AllowlistedRoot, re-verified on every call."""
    if not root.enabled:
        raise PathSecurityError("Allowlisted root is disabled")
    resolved = canonical(root.path)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(Path(root.path))):
        # The root was replaced by a link/junction after approval.
        raise PathSecurityError("Allowlisted root no longer resolves to its approved location")
    if not resolved.is_dir():
        raise PathSecurityError("Allowlisted root is not available")
    return resolved


def project_root(project) -> Path:
    base = root_path(project.root)
    parts = split_relative(project.relative_path)
    resolved = canonical(base.joinpath(*parts))
    if not is_within(resolved, base):
        raise PathSecurityError("Project escapes its allowlisted root")
    if not resolved.is_dir():
        raise PathSecurityError("Project directory is not available")
    return resolved


def resolve(project, rel: str | None, *, allow_root: bool = True, allow_protected: bool = False,
            base: Path | None = None) -> Path:
    """Resolve ``rel`` inside the project. Raises PathSecurityError on escape."""
    base = base or project_root(project)
    parts = split_relative(rel)
    if not parts and not allow_root:
        raise PathSecurityError("Operation not allowed on the project root")
    if not allow_protected and parts and parts[0].lower() in PROTECTED_DIRS:
        raise PathSecurityError(".git internals are protected")
    target = canonical(base.joinpath(*parts))
    if not is_within(target, base):
        raise PathSecurityError("Path escapes the project root (symlink/junction or traversal)")
    return target


def to_relative(base: Path, target: Path) -> str:
    return Path(os.path.relpath(target, base)).as_posix() if target != base else ""
