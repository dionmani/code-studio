from __future__ import annotations

from pathlib import Path

from django.http import Http404

from .models import Project
from .paths import PathSecurityError, project_root


def get_project_for(user, project_id) -> Project:
    """Fetch a project the user may access; 404 otherwise (no existence leak)."""
    try:
        project = Project.objects.select_related("root").get(pk=int(project_id))
    except (Project.DoesNotExist, ValueError, TypeError):
        raise Http404("Project not found")
    if not project.user_can_access(user):
        raise Http404("Project not found")
    return project


def detect_git_mode(path: Path) -> str:
    """Git mode iff a .git directory exists at the project root. Never inits."""
    git = path / ".git"
    return Project.GIT if git.is_dir() and (git / "HEAD").is_file() else Project.UNMANAGED


def project_dict(project: Project, include_path: bool = True) -> dict:
    data = {
        "id": project.id,
        "name": project.name,
        "root_alias": project.root.alias,
        "relative_path": project.relative_path,
        "git_mode": project.git_mode,
        "created_at": project.created_at.isoformat(),
        "available": True,
    }
    try:
        path = project_root(project)
        if include_path:
            data["path"] = str(path)
    except PathSecurityError as exc:
        data["available"] = False
        data["unavailable_reason"] = str(exc)
    return data
