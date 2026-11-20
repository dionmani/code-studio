from __future__ import annotations

import os

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.log import record
from core.api import ApiError, api_view, json_body

from .models import AllowlistedRoot, Project
from .paths import (PathSecurityError, canonical, is_within, normalize_relative, root_path,
                    split_relative, validate_root_path)
from .service import detect_git_mode, get_project_for, project_dict


def _root_dict(root: AllowlistedRoot) -> dict:
    available = True
    try:
        root_path(root)
    except PathSecurityError:
        available = False
    return {"id": root.id, "alias": root.alias, "path": root.path, "description": root.description,
            "enabled": root.enabled, "available": available, "project_count": root.projects.count()}


@api_view(["GET", "POST"])
def roots(request):
    if request.method == "GET":
        qs = AllowlistedRoot.objects.all()
        if not request.user.is_staff:
            qs = qs.filter(enabled=True)
        return {"roots": [_root_dict(r) for r in qs], "can_manage": request.user.is_staff}
    if not request.user.is_staff:
        record(request=request, action="root.create", outcome="denied")
        raise ApiError("Administrator permission required", 403)
    data = json_body(request)
    alias = (data.get("alias") or "").strip()
    try:
        path = validate_root_path((data.get("path") or "").strip())
    except PathSecurityError as exc:
        record(request=request, action="root.create", target=data.get("path", ""), outcome="denied", reason=str(exc))
        raise ApiError(str(exc), 400)
    root = AllowlistedRoot(alias=alias, path=str(path), description=(data.get("description") or "")[:255],
                           created_by=request.user)
    try:
        root.full_clean()
        root.save()
    except Exception as exc:  # ValidationError / IntegrityError
        raise ApiError(f"Invalid root: {exc}")
    record(request=request, action="root.create", target=str(path), alias=alias)
    return _root_dict(root)


@api_view(["DELETE", "PATCH"], staff=True)
def root_detail(request, root_id):
    try:
        root = AllowlistedRoot.objects.get(pk=root_id)
    except AllowlistedRoot.DoesNotExist:
        raise ApiError("Root not found", 404)
    if request.method == "PATCH":
        data = json_body(request)
        if "enabled" in data:
            root.enabled = bool(data["enabled"])
            root.save(update_fields=["enabled"])
        record(request=request, action="root.update", target=root.path, enabled=root.enabled)
        return _root_dict(root)
    if root.projects.exists():
        raise ApiError("Unregister the projects under this root first", 409)
    record(request=request, action="root.delete", target=root.path)
    root.delete()
    return {"deleted": True}


@api_view(["GET"])
def root_browse(request, root_id):
    """List sub-directories of an allowlisted root (for project registration)."""
    try:
        root = AllowlistedRoot.objects.get(pk=root_id, enabled=True)
    except AllowlistedRoot.DoesNotExist:
        raise ApiError("Root not found", 404)
    try:
        base = root_path(root)
        parts = split_relative(request.GET.get("path", ""))
        target = canonical(base.joinpath(*parts))
        if not is_within(target, base) or not target.is_dir():
            raise PathSecurityError("Directory escapes the allowlisted root or does not exist")
    except PathSecurityError as exc:
        raise ApiError(str(exc), 403)
    dirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name == ".git" or not entry.is_dir(follow_symlinks=True):
                    continue
                child = canonical(entry.path)
                if not is_within(child, base):
                    continue
                dirs.append({"name": entry.name, "is_git": (child / ".git").is_dir()})
    except OSError as exc:
        raise ApiError(f"Cannot list directory: {exc.strerror}", 400)
    dirs.sort(key=lambda d: d["name"].lower())
    return {"root": root.alias, "path": "/".join(parts), "dirs": dirs[:1000]}


@api_view(["GET", "POST"])
def projects(request):
    if request.method == "GET":
        qs = Project.objects.select_related("root")
        if not request.user.is_superuser:
            qs = qs.filter(Q(owner=request.user) | Q(members=request.user)).distinct()
        return {"projects": [project_dict(p) for p in qs]}
    if not request.user.is_staff:
        record(request=request, action="project.register", outcome="denied")
        raise ApiError("Administrator permission required to register projects", 403)
    data = json_body(request)
    try:
        root = AllowlistedRoot.objects.get(pk=data.get("root_id"), enabled=True)
    except (AllowlistedRoot.DoesNotExist, ValueError, TypeError):
        raise ApiError("Unknown or disabled allowlisted root", 400)
    try:
        rel = normalize_relative(data.get("relative_path") or "")
        base = root_path(root)
        target = canonical(base.joinpath(*split_relative(rel)))
        if not is_within(target, base):
            raise PathSecurityError("Project path escapes the allowlisted root")
        if not target.is_dir():
            raise PathSecurityError("Project directory does not exist")
    except PathSecurityError as exc:
        record(request=request, action="project.register", target=str(data.get("relative_path")), outcome="denied",
               reason=str(exc))
        raise ApiError(str(exc), 400)
    name = (data.get("name") or "").strip() or (target.name or root.alias)
    project = Project(name=name[:128], root=root, relative_path=rel, git_mode=detect_git_mode(target),
                      owner=request.user)
    try:
        with transaction.atomic():
            project.save()
    except IntegrityError:
        raise ApiError("This folder is already registered as a project", 409)
    project.members.add(request.user)
    record(request=request, action="project.register", project=project, target=str(target), git_mode=project.git_mode)
    return project_dict(project)


@api_view(["GET", "DELETE"])
def project_detail(request, project_id):
    project = get_project_for(request.user, project_id)
    if request.method == "DELETE":
        if not request.user.is_staff:
            raise ApiError("Administrator permission required", 403)
        record(request=request, action="project.unregister", project=None, target=project.name)
        project.delete()  # registry entry only; files on disk are untouched
        return {"deleted": True}
    return project_dict(project)


@api_view(["POST"])
def project_redetect(request, project_id):
    project = get_project_for(request.user, project_id)
    from .paths import project_root

    try:
        mode = detect_git_mode(project_root(project))
    except PathSecurityError as exc:
        raise ApiError(str(exc), 403)
    if mode != project.git_mode:
        project.git_mode = mode
        project.save(update_fields=["git_mode"])
        record(request=request, action="project.redetect", project=project, git_mode=mode)
    return project_dict(project)
