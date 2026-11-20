"""File-backed recovery snapshots (design gate 5).

Layout (inside the internal data dir, never inside a project tree)::

    <DATA_DIR>/snapshots/<project_id>/<YYYYmmddTHHMMSS>-<8 hex>/
        manifest.json
        files/<relative path>...

Each entry records whether the path existed; restoring re-creates existing
paths and removes paths that did not exist (undoing creations, e.g. agent
added files). A restore first snapshots the current state, so it is itself
undoable. Retention: newest ``SNAPSHOT_RETENTION_COUNT`` per project, and
nothing older than ``SNAPSHOT_RETENTION_DAYS`` (the newest 10 are always kept).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from core.api import ApiError
from projects.paths import is_within, project_root, resolve, split_relative

from .fsutil import atomic_write, sha256_file

SNAP_ID = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")
KEEP_MIN = 10


def _cfg(key):
    return settings.CODE_STUDIO[key]


def snapshots_dir(project) -> Path:
    return Path(settings.DATA_DIR) / "snapshots" / str(project.id)


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def create_snapshot(project, op: str, rel_paths: list[str], user=None, note: str = "") -> str:
    base = project_root(project)
    targets = []
    total = 0
    for rel in rel_paths:
        abs_path = resolve(project, rel, base=base, allow_protected=False)
        exists = abs_path.exists()
        if exists:
            total += _tree_size(abs_path)
        targets.append((rel, abs_path, exists))
    if total > _cfg("SNAPSHOT_MAX_BYTES"):
        raise ApiError(f"Too large to snapshot safely ({total} bytes); operation refused", 413)

    sid = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    sdir = snapshots_dir(project) / sid
    fdir = sdir / "files"
    fdir.mkdir(parents=True)
    entries = []
    for rel, abs_path, exists in targets:
        rel_norm = "/".join(split_relative(rel))
        entry = {"path": rel_norm, "existed": exists, "kind": None, "sha256": None}
        if exists:
            dest = fdir.joinpath(*split_relative(rel_norm))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if abs_path.is_dir():
                entry["kind"] = "dir"
                shutil.copytree(abs_path, dest, symlinks=True)
            else:
                entry["kind"] = "file"
                shutil.copy2(abs_path, dest)
                entry["sha256"] = sha256_file(dest)
        entries.append(entry)
    manifest = {"id": sid, "project_id": project.id, "op": op, "note": note[:500],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "user": getattr(user, "username", None), "entries": entries}
    (sdir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    prune(project)
    return sid


def list_snapshots(project) -> list[dict]:
    d = snapshots_dir(project)
    if not d.is_dir():
        return []
    out = []
    for child in sorted(d.iterdir(), reverse=True):
        mf = child / "manifest.json"
        if SNAP_ID.match(child.name) and mf.is_file():
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except ValueError:
                continue
            out.append({k: m.get(k) for k in ("id", "op", "note", "created_at", "user")} |
                       {"paths": [e["path"] for e in m.get("entries", [])]})
    return out


def _load(project, sid: str) -> tuple[Path, dict]:
    if not isinstance(sid, str) or not SNAP_ID.match(sid):
        raise ApiError("Invalid snapshot id", 400)
    sdir = snapshots_dir(project) / sid
    if not (sdir / "manifest.json").is_file():
        raise ApiError("Snapshot not found", 404)
    return sdir, json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))


def restore_snapshot(project, sid: str, user=None) -> dict:
    sdir, manifest = _load(project, sid)
    base = project_root(project)
    paths = [e["path"] for e in manifest["entries"]]
    pre = create_snapshot(project, "pre-restore", paths, user, note=f"before restoring {sid}")
    _apply_manifest(project, base, sdir, manifest)
    return {"restored": sid, "pre_restore_snapshot": pre, "paths": paths}


def _apply_manifest(project, base: Path, sdir: Path, manifest: dict) -> None:
    for entry in manifest["entries"]:
        target = resolve(project, entry["path"], base=base)
        src = (sdir / "files").joinpath(*split_relative(entry["path"]))
        if not is_within(src, sdir):
            raise ApiError("Corrupt snapshot", 500)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if entry["existed"]:
            if entry["kind"] == "dir":
                shutil.copytree(src, target, symlinks=True)
            else:
                atomic_write(target, src.read_bytes())
                shutil.copystat(src, target)


def rollback_to(project, sid: str) -> None:
    """Internal rollback used when a multi-file apply fails midway."""
    sdir, manifest = _load(project, sid)
    _apply_manifest(project, project_root(project), sdir, manifest)


def prune(project) -> int:
    d = snapshots_dir(project)
    if not d.is_dir():
        return 0
    snaps = sorted((c for c in d.iterdir() if SNAP_ID.match(c.name)), reverse=True)
    cutoff = time.time() - _cfg("SNAPSHOT_RETENTION_DAYS") * 86400
    removed = 0
    for idx, snap in enumerate(snaps):
        too_many = idx >= _cfg("SNAPSHOT_RETENTION_COUNT")
        too_old = idx >= KEEP_MIN and snap.stat().st_mtime < cutoff
        if too_many or too_old:
            shutil.rmtree(snap, ignore_errors=True)
            removed += 1
    return removed
