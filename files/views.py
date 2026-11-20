from __future__ import annotations

import os
import re
import shutil

from django.conf import settings

from audit.log import record
from core.api import ApiError, Conflict, api_view, json_body
from projects.paths import PROTECTED_DIRS, canonical, is_within, project_root, resolve, to_relative
from projects.service import get_project_for

from . import snapshots
from .fsutil import SKIP_DIRS, atomic_write, is_binary, language_for, sha256_bytes, token_hash, version_of


def _cfg(key):
    return settings.CODE_STUDIO[key]


@api_view(["GET"])
def tree(request, project_id):
    project = get_project_for(request.user, project_id)
    base = project_root(project)
    rel = request.GET.get("path", "")
    target = resolve(project, rel, base=base)
    if not target.is_dir():
        raise ApiError("Not a directory", 400)
    entries = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name in PROTECTED_DIRS and target == base:
                    continue
                if entry.name.startswith(".") and ".cs-tmp-" in entry.name:
                    continue
                child_rel = f"{to_relative(base, target)}/{entry.name}".lstrip("/")
                is_link = entry.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(entry.path))
                escapes = False
                if is_link:
                    escapes = not is_within(canonical(entry.path), base)
                try:
                    is_dir = entry.is_dir(follow_symlinks=not escapes)
                    size = None if is_dir else entry.stat(follow_symlinks=not escapes).st_size
                except OSError:
                    is_dir, size = False, None
                entries.append({"name": entry.name, "path": child_rel, "type": "dir" if is_dir else "file",
                                "size": size, "link": is_link, "escapes_root": escapes})
    except OSError as exc:
        raise ApiError(f"Cannot list directory: {exc.strerror}", 400)
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": to_relative(base, target), "entries": entries[:5000], "truncated": len(entries) > 5000}


def _read_payload(base, path) -> dict:
    data = path.read_bytes()
    version = version_of(path, data)
    rel = to_relative(base, path)
    out = {"path": rel, "name": path.name, "size": len(data), "version": version, "language": language_for(path.name)}
    if len(data) > _cfg("MAX_EDIT_BYTES"):
        out.update(binary=False, too_large=True, content=None, editable=False)
    elif is_binary(data):
        out.update(binary=True, content=None, editable=False)
    else:
        try:
            out.update(binary=False, content=data.decode("utf-8"), encoding="utf-8", editable=True)
        except UnicodeDecodeError:
            out.update(binary=False, content=data.decode("latin-1"), encoding="latin-1", editable=False,
                       note="File is not valid UTF-8; opened read-only")
    return out


@api_view(["GET", "PUT"])
def file_view(request, project_id):
    project = get_project_for(request.user, project_id)
    base = project_root(project)
    if request.method == "GET":
        path = resolve(project, request.GET.get("path", ""), base=base, allow_root=False)
        if not path.is_file():
            raise ApiError("File not found", 404)
        return _read_payload(base, path)

    data = json_body(request)
    rel = data.get("path", "")
    path = resolve(project, rel, base=base, allow_root=False)
    content = data.get("content")
    if not isinstance(content, str):
        raise ApiError("content must be a string")
    new_bytes = content.encode("utf-8")
    if len(new_bytes) > _cfg("MAX_EDIT_BYTES"):
        raise ApiError("File too large to save from the editor", 413)
    base_version = data.get("base_version")
    force = bool(data.get("force"))
    if path.exists() and not path.is_file():
        raise ApiError("Target is not a regular file", 400)

    if path.exists():
        current_bytes = path.read_bytes()
        current = version_of(path, current_bytes)
        expected_hash = token_hash(base_version)
        changed_on_disk = expected_hash is None or expected_hash != current["sha256"]
        if changed_on_disk and not force:
            record(request=request, action="file.save", project=project, target=rel, outcome="conflict")
            disk = None if is_binary(current_bytes) else current_bytes.decode("utf-8", errors="replace")
            raise Conflict("File changed on disk since it was opened", code="conflict",
                           current_version=current, current_content=disk)
        if force and changed_on_disk:
            # Overwriting someone else's change: keep a recovery copy.
            snapshots.create_snapshot(project, "force-save", [rel], request.user,
                                      note="disk version before conflicting overwrite")
        if current["sha256"] == sha256_bytes(new_bytes):
            return {"path": rel, "version": current, "unchanged": True}
    elif base_version and not force:
        record(request=request, action="file.save", project=project, target=rel, outcome="conflict")
        raise Conflict("File was deleted on disk since it was opened", code="deleted", current_version=None,
                       current_content=None)
    atomic_write(path, new_bytes)
    version = version_of(path, new_bytes)
    record(request=request, action="file.save", project=project, target=rel, forced=force, size=len(new_bytes))
    return {"path": to_relative(base, path), "version": version}


@api_view(["POST"])
def file_create(request, project_id):
    project = get_project_for(request.user, project_id)
    data = json_body(request)
    rel = data.get("path", "")
    kind = data.get("kind", "file")
    path = resolve(project, rel, allow_root=False)
    if path.exists():
        raise Conflict("A file or folder with that name already exists")
    if not path.parent.is_dir():
        raise ApiError("Parent folder does not exist", 400)
    if kind == "dir":
        path.mkdir()
    else:
        content = data.get("content", "")
        atomic_write(path, content.encode("utf-8") if isinstance(content, str) else b"")
    record(request=request, action="file.create", project=project, target=rel, kind=kind)
    out = {"path": to_relative(project_root(project), path), "type": "dir" if kind == "dir" else "file"}
    if kind != "dir":
        out["version"] = version_of(path)
    return out


@api_view(["POST"])
def file_rename(request, project_id):
    project = get_project_for(request.user, project_id)
    data = json_body(request)
    base = project_root(project)
    src = resolve(project, data.get("path", ""), base=base, allow_root=False)
    dst = resolve(project, data.get("new_path", ""), base=base, allow_root=False)
    if not src.exists():
        raise ApiError("Source does not exist", 404)
    if dst.exists():
        raise Conflict("Destination already exists")
    if src.is_dir() and is_within(dst, src):
        raise ApiError("Cannot move a folder into itself")
    if not dst.parent.is_dir():
        raise ApiError("Destination folder does not exist", 400)
    os.rename(src, dst)
    record(request=request, action="file.rename", project=project, target=data.get("path"),
           new_path=data.get("new_path"))
    return {"path": to_relative(base, dst)}


@api_view(["POST"])
def file_delete(request, project_id):
    project = get_project_for(request.user, project_id)
    data = json_body(request)
    rel = data.get("path", "")
    if data.get("confirm") is not True:
        raise ApiError("Deletion requires explicit confirmation (confirm: true)", 428)
    path = resolve(project, rel, allow_root=False)
    if not path.exists():
        raise ApiError("Not found", 404)
    sid = snapshots.create_snapshot(project, "delete", [rel], request.user)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    record(request=request, action="file.delete", project=project, target=rel, snapshot=sid)
    return {"deleted": rel, "snapshot": sid}


@api_view(["GET"])
def search(request, project_id):
    project = get_project_for(request.user, project_id)
    base = project_root(project)
    q = request.GET.get("q", "")
    if not q or len(q) > 500:
        raise ApiError("Query must be 1-500 characters")
    use_regex = request.GET.get("regex") == "1"
    case = request.GET.get("case") == "1"
    flags = 0 if case else re.IGNORECASE
    try:
        pattern = re.compile(q if use_regex else re.escape(q), flags)
    except re.error as exc:
        raise ApiError(f"Invalid regular expression: {exc}")
    max_results = _cfg("SEARCH_MAX_RESULTS")
    results, files_scanned, truncated = [], 0, False
    for dirpath, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fname in sorted(files):
            full = os.path.join(dirpath, fname)
            rel = to_relative(base, canonical(full)) if not os.path.islink(full) else None
            if rel is None:
                continue
            if pattern.search(fname):
                results.append({"path": rel, "line": 0, "text": f"(file name) {fname}"})
            try:
                if os.path.getsize(full) > 1024 * 1024:
                    continue
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            files_scanned += 1
            if is_binary(data):
                continue
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    results.append({"path": rel, "line": lineno, "text": line.strip()[:300]})
                    if len(results) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break
    return {"query": q, "results": results, "files_scanned": files_scanned, "truncated": truncated}


@api_view(["GET"])
def snapshot_list(request, project_id):
    project = get_project_for(request.user, project_id)
    return {"snapshots": snapshots.list_snapshots(project)}


@api_view(["POST"])
def snapshot_restore(request, project_id, snapshot_id):
    project = get_project_for(request.user, project_id)
    data = json_body(request)
    if data.get("confirm") is not True:
        raise ApiError("Restore requires explicit confirmation (confirm: true)", 428)
    result = snapshots.restore_snapshot(project, snapshot_id, request.user)
    record(request=request, action="snapshot.restore", project=project, target=snapshot_id,
           pre_restore=result["pre_restore_snapshot"])
    return result
