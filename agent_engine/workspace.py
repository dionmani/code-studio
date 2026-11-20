"""Isolated agent workspaces and change-set computation.

For every task the engine creates, under its own data dir::

    workspaces/<task_id>/base/   pristine copy of the project at task start
    workspaces/<task_id>/work/   copy the agent runs in (cwd)

Both unmanaged folders and Git repositories use a staging copy of the
*working tree* (including uncommitted edits, excluding ``.git``), so the
proposed diff is always relative to what the user actually sees. For Git
projects the HEAD revision is recorded as ``base_revision``. The live project
is never written by the engine; the IDE applies approved changes.

Excluded from copies: VCS/env/cache dirs and sensitive credential files
(``cs_common.redact.is_sensitive_path``), so secrets never reach agents.

``work/.codestudio-task/`` is the task's handoff folder (see ``tasks.write_handoff``):
the full output of every step, rewritten by the engine before each step and
excluded from the change set.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import os
import shutil
import stat
from pathlib import Path

from cs_common.agent_policy import is_excluded
from cs_common.redact import is_sensitive_path

# The task's handoff folder (written by the engine into the staging copy so every step can read the
# full output of earlier steps). It is never copied from a project and never part of a change set.
HANDOFF_DIR = ".codestudio-task"
EXCLUDE_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
                ".pytest_cache", ".ruff_cache", ".next", ".gradle", HANDOFF_DIR}
MAX_INLINE_BYTES = 5 * 1024 * 1024
MAX_DIFF_CHARS = 400_000


class WorkspaceError(Exception):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: Path):
    """Yield (relposix, abs_path) for regular files, skipping links/excluded."""
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS
                   and not os.path.islink(os.path.join(dirpath, d))
                   and not (hasattr(os.path, "isjunction") and os.path.isjunction(os.path.join(dirpath, d)))]
        for name in files:
            full = Path(dirpath) / name
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = full.relative_to(root).as_posix()
            if is_sensitive_path(rel):
                continue
            yield rel, full


def create_workspace(source: Path, dest_root: Path, max_bytes: int, max_files: int,
                     exclude: list[str] | None = None) -> dict:
    """``exclude``: the project's own patterns for files agents must never see (data exports etc.)."""
    base_dir = dest_root / "base"
    work_dir = dest_root / "work"
    if dest_root.exists():
        raise WorkspaceError("Workspace already exists")
    base_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    manifest: dict[str, str] = {}
    total = 0
    skipped = 0
    for rel, full in iter_files(source):
        if exclude and is_excluded(rel, exclude):
            skipped += 1
            continue
        size = full.stat().st_size
        total += size
        if total > max_bytes or len(manifest) >= max_files:
            raise WorkspaceError(f"Project too large for a staging copy (limit {max_bytes // (1024 * 1024)} MB / "
                                 f"{max_files} files)")
        for d in (base_dir, work_dir):
            target = d / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full, target)
        manifest[rel] = _sha(base_dir / rel)
    return {"base": str(base_dir), "work": str(work_dir), "manifest": manifest, "bytes": total, "excluded": skipped}


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _text(data: bytes | None) -> str | None:
    if data is None:
        return ""
    if _is_binary(data):
        return None
    return data.decode("utf-8", errors="replace")


def compute_changes(base_dir: Path, work_dir: Path, manifest: dict[str, str]) -> list[dict]:
    changes = []
    seen = set()
    # Include sensitive files too so they can be *reported* (and refused).
    work_files: dict[str, Path] = {}
    for dirpath, dirs, files in os.walk(work_dir, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        for name in files:
            full = Path(dirpath) / name
            if stat.S_ISREG(os.lstat(full).st_mode):
                work_files[full.relative_to(work_dir).as_posix()] = full
    for rel, full in sorted(work_files.items()):
        seen.add(rel)
        new_sha = _sha(full)
        base_sha = manifest.get(rel)
        if base_sha == new_sha:
            continue
        action = "modified" if base_sha else "added"
        changes.append(_change(rel, action, base_dir / rel if base_sha else None, full, base_sha, new_sha))
    for rel, base_sha in sorted(manifest.items()):
        if rel not in seen:
            changes.append(_change(rel, "deleted", base_dir / rel, None, base_sha, None))
    return changes


def _change(rel, action, base_path: Path | None, new_path: Path | None, base_sha, new_sha) -> dict:
    old = base_path.read_bytes() if base_path else None
    new = new_path.read_bytes() if new_path else None
    o_text, n_text = _text(old), _text(new)
    binary = o_text is None or n_text is None
    if binary:
        diff = f"Binary files a/{rel} and b/{rel} differ\n"
    else:
        diff = "".join(difflib.unified_diff(
            o_text.splitlines(keepends=True), n_text.splitlines(keepends=True),
            fromfile=f"a/{rel}" if old is not None else "/dev/null",
            tofile=f"b/{rel}" if new is not None else "/dev/null"))
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]\n"
    entry = {
        "path": rel, "action": action, "base_sha256": base_sha, "new_sha256": new_sha, "binary": binary,
        "size": len(new) if new is not None else 0, "sensitive": is_sensitive_path(rel), "diff": diff,
        "too_large": bool(new is not None and len(new) > MAX_INLINE_BYTES),
    }
    if new is not None and len(new) <= MAX_INLINE_BYTES:
        entry["content_b64"] = base64.b64encode(new).decode("ascii")
    if not binary and old is not None and len(old) <= MAX_INLINE_BYTES:
        entry["base_text"] = o_text
    if not binary and new is not None and len(new) <= MAX_INLINE_BYTES:
        entry["new_text"] = n_text
    return entry


def remove_workspace(dest_root: Path) -> None:
    def onerror(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    if dest_root.exists():
        shutil.rmtree(dest_root, onexc=lambda f, p, e: onerror(f, p, e))
