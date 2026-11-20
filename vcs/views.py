from __future__ import annotations

import re

from django.utils import timezone

from audit.log import record
from core.api import ApiError, api_view, json_body
from projects.service import get_project_for

from . import service
from .models import CommitMessagePreference, GitCredential
from .secrets import SealError, seal, unseal


def _identity(user):
    name = user.get_full_name() or user.username
    email = user.email or f"{user.username}@localhost"
    return name, email


@api_view(["GET"])
def git_status(request, project_id):
    project = get_project_for(request.user, project_id)
    if project.git_mode != "git":
        # Unmanaged folders: no Git prompts, no errors.
        return {"enabled": False, "mode": "unmanaged"}
    return service.status(project)


@api_view(["GET"])
def git_diff(request, project_id):
    project = get_project_for(request.user, project_id)
    return service.diff(project, request.GET.get("path", ""), staged=request.GET.get("staged") == "1")


def _paths(data):
    paths = data.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise ApiError("paths must be a non-empty list of strings")
    return paths[:1000]


@api_view(["POST"])
def git_stage(request, project_id):
    project = get_project_for(request.user, project_id)
    paths = _paths(json_body(request))
    service.stage(project, paths)
    record(request=request, action="git.stage", project=project, target=",".join(paths)[:1000])
    return service.status(project)


@api_view(["POST"])
def git_unstage(request, project_id):
    project = get_project_for(request.user, project_id)
    paths = _paths(json_body(request))
    service.unstage(project, paths)
    record(request=request, action="git.unstage", project=project, target=",".join(paths)[:1000])
    return service.status(project)


@api_view(["POST"])
def git_commit(request, project_id):
    project = get_project_for(request.user, project_id)
    data = json_body(request)
    name, email = _identity(request.user)
    sha = service.commit(project, (data.get("message") or "")[:10000], name, email)
    record(request=request, action="git.commit", project=project, target=sha)
    return {"commit": sha, "status": service.status(project)}


_MESSAGE_PROMPT = """You write Git commit messages. Reply with ONLY the commit message: no preamble, \
no quotes, no code fences, no explanation.

Format: a summary line in the imperative mood, at most 72 characters, no trailing period. If the change \
needs more explanation, add a blank line and then a short body (wrapped at 72 characters, bullet points \
are fine) saying what changed and why. Keep small changes to the summary line alone.
{style}
Files ({which}):
{files}

Diff:
{diff}"""


@api_view(["POST"])
def git_commit_message(request, project_id):
    """Ask an AI agent (through the Agent Engine) for a commit message describing the staged
    changes, or all working-tree changes when nothing is staged. Nothing is committed."""
    from agents_client.engine_client import EngineClient, EngineError

    project = get_project_for(request.user, project_id)
    if project.git_mode != "git":
        raise ApiError("This project is not a Git repository", 400)
    data = json_body(request)
    pref = CommitMessagePreference.objects.filter(user=request.user).first()
    # an explicit request value wins over the saved preference
    adapter = str(data.get("adapter") or (pref.adapter if pref else "") or "").strip()[:64] or None
    model = str(data.get("model") or (pref.model if pref else "") or "").strip()[:100] or None
    if model and not _MODEL.match(model):
        raise ApiError("That model name is not valid")
    changes = service.changes_for_message(project)
    subjects = service.recent_subjects(project)
    style = ("\nMatch the style of this repository's recent commit subjects:\n"
             + "\n".join(f"- {s}" for s in subjects) + "\n") if subjects else ""
    files = "\n".join(f"{f['status']}: {f['path']}" for f in changes["files"][:300])
    diff = changes["diff"] + (f"\n[{changes['omitted']} more file diffs omitted for length]\n"
                              if changes["omitted"] else "")
    prompt = _MESSAGE_PROMPT.format(style=style, which="staged" if changes["staged"] else "not staged yet",
                                    files=files, diff=diff)
    try:
        result = EngineClient().request("POST", "/generate", {"prompt": prompt, "adapter": adapter, "model": model,
                                                               "timeout_seconds": 120}, timeout=150)
    except EngineError as exc:
        record(request=request, action="git.commit_message", project=project, outcome="error", error=str(exc))
        status = exc.status if exc.status in (400, 404, 409, 503) else 502
        raise ApiError(str(exc).replace("Engine error 409: ", ""), status)
    record(request=request, action="git.commit_message", project=project, target=result.get("adapter") or "")
    return {"message": result["text"], "adapter": result.get("adapter"), "adapter_name": result.get("adapter_name"),
            "model": model, "staged": changes["staged"]}


# Agents that can write commit messages (never the checks-only "verify" adapter).
_NOT_WRITERS = {"verify"}
# What the engine uses when no model is chosen (agent_engine/generate.py DEFAULT_MODELS).
_DEFAULT_MODELS = {"claude-code": "haiku"}
_MODEL_SUGGESTIONS = {"claude-code": ["haiku", "sonnet", "opus"]}
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}$")


@api_view(["GET", "PATCH"])
def commit_message_preference(request):
    """The signed-in user's choice of agent/model for AI commit messages."""
    from agents_client.models import AgentAdapter

    writers = [a for a in AgentAdapter.objects.all() if a.identifier not in _NOT_WRITERS]
    pref = CommitMessagePreference.objects.filter(user=request.user).first()
    if request.method == "PATCH":
        data = json_body(request)
        adapter = str(data.get("adapter") or "").strip()
        model = str(data.get("model") or "").strip()
        if adapter and adapter not in {a.identifier for a in writers}:
            raise ApiError("Unknown agent")
        if model and not _MODEL.match(model):
            raise ApiError("Enter a model name such as haiku or sonnet (letters, digits, . _ - : / only)")
        pref = pref or CommitMessagePreference(user=request.user)
        pref.adapter, pref.model = adapter, model
        pref.save()
        record(request=request, action="git.commit_message.preference", target=f"{adapter or 'auto'}/{model or 'default'}")
    return {"adapter": pref.adapter if pref else "", "model": pref.model if pref else "",
            "agents": [{"identifier": a.identifier, "display_name": a.display_name or a.identifier,
                        "available": a.available, "default_model": _DEFAULT_MODELS.get(a.identifier, ""),
                        "suggested_models": _MODEL_SUGGESTIONS.get(a.identifier, [])} for a in writers],
            "automatic_order": ["claude-code", "codex", "antigravity"]}


# ---- credentials ------------------------------------------------------------

def _credentials_for(user, url: str) -> tuple[service.Credentials | None, GitCredential | None]:
    host = service.remote_host(url)
    if not host:
        return None, None
    cred = GitCredential.objects.filter(user=user, host=host).first()
    if cred is None:
        return None, None
    try:
        token = unseal(cred.token_sealed)
    except SealError as exc:
        raise ApiError(f"Saved token for {host} can't be read ({exc}); please enter it again in Git settings", 409)
    return service.Credentials(cred.username, token, f"{cred.username}@{host}"), cred


def _remote_op(request, project_id, op):
    project = get_project_for(request.user, project_id)
    data = json_body(request)
    if data.get("confirm") is not True:
        raise ApiError(f"git {op} requires explicit confirmation (confirm: true)", 428)
    remote = data.get("remote") or "origin"
    fn = service.push if op == "push" else service.pull
    try:
        url = service.remote_url_for(project, remote)
        creds, cred_row = _credentials_for(request.user, url)
        result = fn(project, remote, creds)
    except ApiError as exc:
        record(request=request, action=f"git.{op}", project=project, target=remote, outcome="error", error=str(exc))
        raise
    except Exception as exc:  # transport errors from dulwich
        record(request=request, action=f"git.{op}", project=project, target=remote, outcome="error", error=str(exc))
        raise ApiError(f"git {op} failed: {exc}", 502)
    if cred_row is not None:
        GitCredential.objects.filter(pk=cred_row.pk).update(last_used_at=timezone.now())
    record(request=request, action=f"git.{op}", project=project, target=remote,
           credential=creds.label if creds else None)
    return result


@api_view(["POST"])
def git_push(request, project_id):
    return _remote_op(request, project_id, "push")


@api_view(["POST"])
def git_pull(request, project_id):
    return _remote_op(request, project_id, "pull")


_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$")


def _cred_dict(c: GitCredential) -> dict:
    return {"id": c.id, "host": c.host, "username": c.username, "token_hint": c.token_hint,
            "updated_at": c.updated_at.isoformat(), "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None}


@api_view(["GET", "POST"])
def credentials(request):
    """The signed-in user's own Git logins. Tokens are write-only."""
    if request.method == "GET":
        return {"credentials": [_cred_dict(c) for c in GitCredential.objects.filter(user=request.user)]}
    data = json_body(request)
    host = str(data.get("host") or "").strip().lower()
    host = re.sub(r"^https?://", "", host).split("/")[0]
    username = str(data.get("username") or "").strip()
    token = str(data.get("token") or "").strip()
    if not _HOST.match(host):
        raise ApiError("Enter a host name such as github.com")
    if not username or len(username) > 128 or any(ch.isspace() for ch in username):
        raise ApiError("Enter the username for that host (no spaces)")
    existing = GitCredential.objects.filter(user=request.user, host=host).first()
    if not token and existing is None:
        raise ApiError("Enter an access token")
    if len(token) > 1000 or any(ch.isspace() for ch in token):
        raise ApiError("The token looks malformed (it should be one word with no spaces)")
    if existing is None:
        existing = GitCredential(user=request.user, host=host)
    existing.username = username
    if token:  # blank token on an existing entry = keep the stored one
        existing.token_sealed = seal(token)
        existing.token_hint = token[-4:] if len(token) >= 12 else ""
    existing.save()
    record(request=request, action="git.credential.save", target=f"{username}@{host}")
    return _cred_dict(existing)


@api_view(["DELETE"])
def credential_detail(request, credential_id):
    cred = GitCredential.objects.filter(user=request.user, pk=credential_id).first()
    if cred is None:
        raise ApiError("Credential not found", 404)
    record(request=request, action="git.credential.delete", target=f"{cred.username}@{cred.host}")
    cred.delete()
    return {"deleted": True}


@api_view(["GET", "PATCH"])
def identity(request):
    """Name/email used as author for commits made in Code Studio."""
    user = request.user
    if request.method == "PATCH":
        data = json_body(request)
        name = str(data.get("name") or "").strip()[:150]
        email = str(data.get("email") or "").strip()[:254]
        if email and not re.match(r"^[^@\s<>]+@[^@\s<>]+$", email):
            raise ApiError("Enter a valid email address")
        if any(c in name for c in "<>\n"):
            raise ApiError("Name may not contain < > or line breaks")
        user.first_name, user.last_name, user.email = name, "", email
        user.save(update_fields=["first_name", "last_name", "email"])
        record(request=request, action="git.identity.update")
    name, email = _identity(user)
    return {"name": user.get_full_name(), "email": user.email, "effective": f"{name} <{email}>"}


# ---- per-project Git settings -------------------------------------------------

@api_view(["GET", "PATCH"])
def git_settings(request, project_id):
    project = get_project_for(request.user, project_id)
    if project.git_mode != "git":
        raise ApiError("This project is not a Git repository", 400)
    if request.method == "PATCH":
        if not request.user.is_staff:
            raise ApiError("Only administrators can change project settings", 403)
        value = json_body(request).get("line_endings")
        if value not in dict(project.LINE_ENDINGS):
            raise ApiError("line_endings must be auto, crlf or lf")
        if value != project.git_line_endings:
            project.git_line_endings = value
            project.save(update_fields=["git_line_endings"])
            service.invalidate_stat_cache(project)
        record(request=request, action="git.settings.update", project=project, line_endings=value)
    repo, _base = service.open_repo(project)
    with repo:
        info = service.branch_info(repo)
        le = repo.line_endings
    remotes = []
    for name, url in info["remotes"].items():
        host = service.remote_host(url)
        cred = GitCredential.objects.filter(user=request.user, host=host).first() if host else None
        remotes.append({"name": name, "url": url, "host": host, "https": bool(host),
                        "credential": _cred_dict(cred) if cred else None})
    return {"line_endings": le, "remotes": remotes, "choices": project.LINE_ENDINGS}


@api_view(["POST"])
def git_check_access(request, project_id):
    project = get_project_for(request.user, project_id)
    remote = json_body(request).get("remote") or "origin"
    url = service.remote_url_for(project, remote)
    creds, _row = _credentials_for(request.user, url)
    result = service.check_access(url, creds)
    result["credential"] = creds.label if creds else None
    record(request=request, action="git.check_access", project=project, target=remote,
           read=result.get("read"), write=result.get("write"))
    return result
