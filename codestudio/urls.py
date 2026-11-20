from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from agents_client import views as agents
from core import views as core
from files import views as files
from localinstall import views as localinstall
from projects import views as projects
from vcs import views as vcs

P = "api/projects/<int:project_id>/"

urlpatterns = [
    path("", core.ide, name="ide"),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
    # run locally / versions
    path("local/", localinstall.local_page, name="local"),
    path("local/download", localinstall.download, name="local-download"),
    path("api/version", localinstall.version),
    path("api/local/update-status", localinstall.update_status),
    # projects / roots
    path("api/roots", projects.roots),
    path("api/roots/<int:root_id>", projects.root_detail),
    path("api/roots/<int:root_id>/browse", projects.root_browse),
    path("api/projects", projects.projects),
    path("api/projects/<int:project_id>", projects.project_detail),
    path(P + "redetect", projects.project_redetect),
    # files
    path(P + "tree", files.tree),
    path(P + "file", files.file_view),
    path(P + "file/create", files.file_create),
    path(P + "file/rename", files.file_rename),
    path(P + "file/delete", files.file_delete),
    path(P + "search", files.search),
    path(P + "snapshots", files.snapshot_list),
    path(P + "snapshots/<str:snapshot_id>/restore", files.snapshot_restore),
    # git
    path(P + "git/status", vcs.git_status),
    path(P + "git/diff", vcs.git_diff),
    path(P + "git/stage", vcs.git_stage),
    path(P + "git/unstage", vcs.git_unstage),
    path(P + "git/commit", vcs.git_commit),
    path(P + "git/commit-message", vcs.git_commit_message),
    path(P + "git/pull", vcs.git_pull),
    path(P + "git/push", vcs.git_push),
    path(P + "git/settings", vcs.git_settings),
    path(P + "git/check-access", vcs.git_check_access),
    path("api/git/credentials", vcs.credentials),
    path("api/git/credentials/<int:credential_id>", vcs.credential_detail),
    path("api/git/identity", vcs.identity),
    path("api/git/commit-message-preference", vcs.commit_message_preference),
    # agents / engine
    path("api/engine/status", agents.engine_status),
    path("api/agents", agents.agents),
    path("api/agents/<str:identifier>/enabled", agents.agent_enabled),
    path("api/profiles", agents.profiles),
    path("api/team", agents.default_team),
    path("api/profiles/<int:profile_id>", agents.profile_detail),
    path(P + "tasks", agents.project_tasks),
    path(P + "checks", agents.project_checks),
    path("api/tasks/<uuid:task_id>", agents.task_detail),
    path("api/tasks/<uuid:task_id>/events", agents.task_events),
    path("api/tasks/<uuid:task_id>/cancel", agents.task_cancel),
    path("api/tasks/<uuid:task_id>/followup", agents.task_followup),
    path("api/tasks/<uuid:task_id>/images/<str:name>", agents.task_image),
    path("api/tasks/<uuid:task_id>/changes", agents.task_changes),
    path("api/tasks/<uuid:task_id>/approve", agents.task_approve),
    path("api/tasks/<uuid:task_id>/reject", agents.task_reject),
    path("api/audit", agents.audit_list),
]
