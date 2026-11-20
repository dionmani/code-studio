from __future__ import annotations

import asyncio
import json

from asgiref.sync import sync_to_async
from django.db.models import Count
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

from audit.log import record
from cs_common.agent_policy import PolicyError, clean_allowed_commands, clean_check_env, clean_excludes
from cs_common.images import STORED_NAME, ImageError, clean_images, save_images, step_prefix, to_payload
from core.api import ApiError, api_view, json_body
from projects.paths import PathSecurityError, project_root, resolve
from projects.service import get_project_for

from . import service
from .engine_client import EngineClient, EngineError
from .models import AgentAdapter, AgentProfile, DefaultTeam, Task, TaskAssignment, TaskEvent


def _engine_error(exc: EngineError):
    raise ApiError(str(exc), exc.status if exc.status in (400, 404, 409, 503) else 502, engine_online=exc.status != 503)


@api_view(["GET"])
def engine_status(request):
    return EngineClient().health(use_cache=request.GET.get("fresh") != "1")


def _profile_dict(p: AgentProfile) -> dict:
    return {"id": p.id, "name": p.name, "adapter": p.adapter.identifier, "adapter_name": p.adapter.display_name,
            "adapter_available": p.adapter.available, "role": p.role, "execution_policy": p.execution_policy,
            "enabled": p.enabled, "task_count": getattr(p, "task_count", None)}


def _profiles_qs():
    return AgentProfile.objects.select_related("adapter").annotate(task_count=Count("taskassignment"))


def _require_staff(request):
    if not request.user.is_staff:
        raise ApiError("Only administrators can change agent profiles", 403)


def _profile_name(raw, exclude_id=None) -> str:
    name = str(raw or "").strip()[:128]
    if not name:
        raise ApiError("Profile name is required")
    if AgentProfile.objects.filter(name__iexact=name).exclude(pk=exclude_id).exists():
        raise ApiError(f"A profile named \"{name}\" already exists", 409)
    return name


def _profile_role(raw) -> str:
    role = str(raw or "").strip()[:64]
    if not role:
        raise ApiError("Role is required")
    return role


def _profile_adapter(identifier) -> AgentAdapter:
    try:
        return AgentAdapter.objects.get(identifier=identifier)
    except AgentAdapter.DoesNotExist:
        raise ApiError("Unknown agent (open Settings > Agent team while the engine is online)")


@api_view(["GET"])
def agents(request):
    engine_online, error, agents_list = True, None, []
    try:
        agents_list = EngineClient().agents()["agents"]
        service.sync_adapters(agents_list)
    except EngineError as exc:
        engine_online, error = False, str(exc)
        agents_list = [{"identifier": a.identifier, "display_name": a.display_name, "available": False,
                        "version": a.version, "capabilities": (a.capability_snapshot or {}).get("capabilities"),
                        "notes": ["engine offline - last known probe"], "stale": True}
                       for a in AgentAdapter.objects.all()]
    profiles = [_profile_dict(p) for p in _profiles_qs()]
    return {"engine_online": engine_online, "error": error, "adapters": agents_list, "profiles": profiles}


@api_view(["POST"], staff=True)
def agent_enabled(request, identifier):
    """Turn a switchable agent (Antigravity) on or off in the engine. No restart needed."""
    data = json_body(request)
    if not isinstance(data.get("enabled"), bool):
        raise ApiError('Send {"enabled": true} or {"enabled": false}')
    try:
        agent = EngineClient().set_agent_enabled(identifier, data["enabled"])["agent"]
    except EngineError as exc:
        status = exc.status if exc.status in (400, 404, 503) else 502
        raise ApiError(str(exc), status)
    service.sync_adapters([agent])
    record(request=request, action="agent.enabled" if data["enabled"] else "agent.disabled", target=identifier)
    return {"agent": agent}


def _team_steps(user) -> list[dict]:
    """The user's saved lineup, minus profiles that were deleted since."""
    team = DefaultTeam.objects.filter(user=user).first()
    if not team:
        return []
    existing = set(AgentProfile.objects.values_list("pk", flat=True))
    return [s for s in team.steps if s.get("profile_id") in existing]


@api_view(["GET", "PUT"])
def default_team(request):
    """Settings > Default team: the ordered profiles (and roles) a new task starts with."""
    if request.method == "GET":
        return {"steps": _team_steps(request.user)}
    raw = json_body(request).get("steps")
    if not isinstance(raw, list):
        raise ApiError('Send {"steps": [{"profile_id": 1, "role": "implementer"}, ...]}')
    if len(raw) > DefaultTeam.MAX_STEPS:
        raise ApiError(f"A team can have at most {DefaultTeam.MAX_STEPS} steps")
    profiles = {p.pk: p for p in AgentProfile.objects.filter(pk__in=[s.get("profile_id") for s in raw
                                                                       if isinstance(s, dict)])}
    steps, seen = [], set()
    for item in raw:
        pid = item.get("profile_id") if isinstance(item, dict) else None
        profile = profiles.get(pid)
        if profile is None:
            raise ApiError("Unknown profile in the team (it may have been deleted; reload Settings)")
        if not profile.enabled:
            raise ApiError(f'Profile "{profile.name}" is turned off; turn it on or remove it from the team')
        if pid in seen:
            raise ApiError(f'Profile "{profile.name}" is in the team twice')
        seen.add(pid)
        steps.append({"profile_id": pid, "role": _profile_role(item.get("role") or profile.role)})
    DefaultTeam.objects.update_or_create(user=request.user, defaults={"steps": steps})
    record(request=request, action="team.save", target=" > ".join(profiles[s["profile_id"]].name for s in steps))
    return {"steps": steps}


def _int_field(raw, label, lo, hi):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be a whole number")
    if not lo <= value <= hi:
        raise ApiError(f"{label} must be between {lo} and {hi}")
    return value


def _clean_policy(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    timeout = raw.get("timeout_seconds")
    policy = {"read_only": bool(raw.get("read_only")),
              "timeout_seconds": 900 if timeout in (None, "") else _int_field(timeout, "Time limit (seconds)", 5, 3600)}
    model = str(raw.get("model") or "").strip()
    if model:
        policy["model"] = model[:128]
    if raw.get("max_turns") not in (None, "", 0):
        policy["max_turns"] = _int_field(raw["max_turns"], "Max turns", 1, 200)
    try:
        allowed = clean_allowed_commands(raw.get("allowed_commands"))
    except PolicyError as exc:
        raise ApiError(str(exc))
    if allowed and not policy["read_only"]:
        policy["allowed_commands"] = allowed
    return policy


def _get_profile(profile_id) -> AgentProfile:
    try:
        return _profiles_qs().get(pk=profile_id)
    except AgentProfile.DoesNotExist:
        raise ApiError("Profile not found", 404)


@api_view(["GET", "POST"])
def profiles(request):
    if request.method == "GET":
        return {"profiles": [_profile_dict(p) for p in _profiles_qs()]}
    _require_staff(request)
    data = json_body(request)
    adapter = _profile_adapter(data.get("adapter"))
    name = _profile_name(data.get("name"))
    role = _profile_role(data.get("role") or "implementer")
    p = AgentProfile.objects.create(name=name, adapter=adapter, role=role, enabled=bool(data.get("enabled", True)),
                                    execution_policy=_clean_policy(data.get("execution_policy")),
                                    created_by=request.user)
    record(request=request, action="profile.create", target=name, adapter=adapter.identifier, role=role)
    return _profile_dict(_get_profile(p.pk))


@api_view(["GET", "PATCH", "DELETE"])
def profile_detail(request, profile_id):
    p = _get_profile(profile_id)
    if request.method == "GET":
        return _profile_dict(p)
    _require_staff(request)
    if request.method == "DELETE":
        record(request=request, action="profile.delete", target=p.name)
        p.delete()  # past task assignments keep their adapter/role (profile link becomes empty)
        return {"deleted": True}
    data = json_body(request)
    changed = []
    if "name" in data:
        p.name = _profile_name(data["name"], exclude_id=p.pk)
        changed.append("name")
    if "adapter" in data:
        p.adapter = _profile_adapter(data["adapter"])
        changed.append("adapter")
    if "role" in data:
        p.role = _profile_role(data["role"])
        changed.append("role")
    if "enabled" in data:
        p.enabled = bool(data["enabled"])
        changed.append("enabled")
    if "execution_policy" in data:
        p.execution_policy = _clean_policy(data["execution_policy"])
        changed.append("execution_policy")
    p.save()
    record(request=request, action="profile.update", target=p.name, fields=changed)
    return _profile_dict(_get_profile(p.pk))


def _images(task_id, records) -> list[dict]:
    return [{"name": r.get("name"), "media_type": r.get("media_type"), "size": r.get("size"),
             "url": f"/api/tasks/{task_id}/images/{r.get('file')}"} for r in records or []]


def _request_images(data: dict) -> list[dict]:
    try:
        return clean_images(data.get("images"))
    except ImageError as exc:
        raise ApiError(str(exc))


def _assignment_dict(a: TaskAssignment, full: bool) -> dict:
    d = {"order": a.order, "role": a.role, "adapter": a.adapter_identifier,
         "profile": a.profile.name if a.profile else None, "status": a.status, "read_only": a.read_only,
         "can_resume": bool(a.session_id), "followup_of": a.followup_of,
         "attachments": _images(a.task_id, a.attachments)}
    if full:  # the post-task summary: what each agent replied
        d.update(message=a.message, result=a.result, error=a.error, exit_code=a.exit_code, stats=a.stats,
                 started_at=a.started_at.isoformat() if a.started_at else None,
                 finished_at=a.finished_at.isoformat() if a.finished_at else None)
    return d


def _task_dict(t: Task, events: bool = False, full: bool = False) -> dict:
    d = {"id": str(t.id), "project_id": t.project_id, "objective": t.objective, "state": t.state,
         "engine_task_id": t.engine_task_id, "error": t.error, "created_at": t.created_at.isoformat(),
         "updated_at": t.updated_at.isoformat(), "requester": t.requester.username if t.requester else None,
         "base_revision": t.base_revision, "last_event_seq": t.last_event_seq,
         "attachments": _images(t.id, t.attachments),
         "assignments": [_assignment_dict(a, full) for a in t.assignments.select_related("profile")]}
    if full:
        d["started_at"] = t.started_at.isoformat() if t.started_at else None
        d["finished_at"] = t.finished_at.isoformat() if t.finished_at else None
        d["changes_summary"] = t.changes_summary
    if t.state in Task.TERMINAL:
        d["follow_up"] = _follow_up_status(t)
    cs = getattr(t, "changeset", None) if hasattr(t, "changeset") else None
    if cs:
        d["changeset"] = {"status": cs.approval_status, "files": len(cs.summary), "snapshot": cs.snapshot_id}
    if events:
        d["events"] = [{"seq": e.sequence, "type": e.type, "payload": e.payload, "ts": e.timestamp.isoformat()}
                       for e in t.events.all()[:2000]]
    return d


FOLLOWUP_STATES = {"awaiting_review", "completed", "failed", "cancelled", "interrupted"}


def _follow_up_status(t: Task) -> dict:
    """Can the user continue one of this task's conversations, and if not, why."""
    if t.state in ("applied", "rejected"):
        what = "applied" if t.state == "applied" else "rejected"
        return {"available": False, "reason": f"The changes were {what}, so the agents' working copy is gone. "
                                              "Start a new task to keep going."}
    if t.state not in FOLLOWUP_STATES:
        return {"available": False, "reason": "The task never started, so there is nothing to continue."}
    if not t.can_follow_up:
        return {"available": False, "reason": "The agents' working copy has expired or was removed. "
                                              "Start a new task to keep going."}
    if not any(a.session_id for a in t.assignments.all()):
        return {"available": False, "reason": "No step reported a conversation the agent can resume."}
    return {"available": True, "reason": None}


# ---- Settings > Project checks -------------------------------------------------------

VENV_CANDIDATES = (".venv", "venv", "env")


def _find_venv(project, setting: str):
    """The project's Python environment: the configured folder, or the first of .venv/venv/env
    that is a virtualenv. Returns (absolute path or None, relative name or None)."""
    names = [setting] if setting else list(VENV_CANDIDATES)
    for name in names:
        try:
            path = resolve(project, name, allow_root=False)
        except PathSecurityError:
            continue
        if (path / "pyvenv.cfg").is_file():
            return path, name
    return None, None


def _checks_payload(project) -> dict:
    """What the engine needs: validated settings with the venv resolved to an absolute path."""
    raw = project.agent_checks or {}
    venv, _ = _find_venv(project, raw.get("python_env") or "") if raw.get("python_env") != "-" else (None, None)
    return {"verify_commands": raw.get("verify_commands") or [], "env": raw.get("env") or {},
            "exclude": raw.get("exclude") or [], "python_env": str(venv) if venv else None}


def _checks_dict(project) -> dict:
    raw = project.agent_checks or {}
    setting = raw.get("python_env") or ""
    venv, name = _find_venv(project, setting) if setting != "-" else (None, None)
    return {"verify_commands": raw.get("verify_commands") or [], "env": raw.get("env") or {},
            "exclude": raw.get("exclude") or [], "python_env": setting,
            "python_env_found": name, "can_edit": None}


@api_view(["GET", "PUT"])
def project_checks(request, project_id):
    """Per-project settings for agents and the Verify step: check commands, forced
    environment (test settings), files never copied to agents, Python environment."""
    project = get_project_for(request.user, project_id)
    if request.method == "PUT":
        _require_staff(request)
        data = json_body(request)
        try:
            checks = {"verify_commands": clean_allowed_commands(data.get("verify_commands")),
                      "env": clean_check_env(data.get("env")), "exclude": clean_excludes(data.get("exclude"))}
        except PolicyError as exc:
            raise ApiError(str(exc))
        venv = str(data.get("python_env") or "").strip().replace("\\", "/").strip("/")
        if venv and venv != "-":
            try:
                path = resolve(project, venv, allow_root=False)
            except PathSecurityError as exc:
                raise ApiError(f"Python environment: {exc}")
            if not (path / "pyvenv.cfg").is_file():
                raise ApiError(f"\"{venv}\" is not a Python virtual environment (no pyvenv.cfg)")
        checks["python_env"] = venv
        project.agent_checks = checks
        project.save(update_fields=["agent_checks"])
        record(request=request, action="project.checks.update", project=project,
               commands=len(checks["verify_commands"]), env=sorted(checks["env"]), exclude=checks["exclude"])
    d = _checks_dict(project)
    d["can_edit"] = request.user.is_staff
    return d


def _get_task(user, task_id) -> Task:
    try:
        task = Task.objects.select_related("project", "project__root", "requester").get(pk=task_id)
    except (Task.DoesNotExist, ValueError, Exception):
        raise Http404("Task not found")
    if not task.project.user_can_access(user):
        raise Http404("Task not found")
    return task


def _base_revision(project) -> str:
    if project.git_mode != "git":
        return ""
    try:
        from vcs.service import open_repo

        repo, _ = open_repo(project)
        with repo:
            return repo.head().decode()
    except Exception:
        return ""


@api_view(["GET", "POST"])
def project_tasks(request, project_id):
    project = get_project_for(request.user, project_id)
    if request.method == "GET":
        tasks = Task.objects.filter(project=project).select_related("requester").prefetch_related("assignments")[:100]
        return {"tasks": [_task_dict(t) for t in tasks]}

    data = json_body(request)
    objective = (data.get("objective") or "").strip()
    if not objective:
        raise ApiError("objective is required")
    if len(objective) > 20000:
        raise ApiError("objective too long (max 20000 chars)")
    images = _request_images(data)
    raw_assign = data.get("assignments")
    if not isinstance(raw_assign, list) or not raw_assign:
        raise ApiError("Pick at least one agent profile")
    assignments = []
    for i, item in enumerate(raw_assign[:10]):
        try:
            prof = AgentProfile.objects.select_related("adapter").get(pk=item.get("profile_id"), enabled=True)
        except (AgentProfile.DoesNotExist, ValueError, TypeError, AttributeError):
            raise ApiError("Unknown or disabled agent profile")
        role = (item.get("role") or prof.role).strip()[:64]
        assignments.append((i, prof, role))

    if any(prof.adapter.identifier == "verify" for _, prof, _ in assignments) \
            and not (project.agent_checks or {}).get("verify_commands"):
        raise ApiError("This lineup has a Verify step, but the project has no check commands. "
                       "Add them in Settings → Project checks.")
    root = project_root(project)  # IDE resolves the canonical path; engine never sees client paths
    task = Task.objects.create(project=project, requester=request.user, objective=objective,
                               base_revision=_base_revision(project))
    for i, prof, role in assignments:
        TaskAssignment.objects.create(task=task, profile=prof, adapter_identifier=prof.adapter.identifier,
                                      role=role, order=i)
    if images:
        task.attachments = save_images(task.attachments_dir, images, "task")
        task.save(update_fields=["attachments"])
    payload = {
        "task_id": task.id.hex, "objective": objective, "requested_by": request.user.username,
        "project": {"id": project.id, "name": project.name, "root": str(root), "git_mode": project.git_mode,
                    "base_revision": task.base_revision or None, "checks": _checks_payload(project)},
        "assignments": [{"assignment_id": i, "profile": prof.name, "adapter": prof.adapter.identifier, "role": role,
                         "policy": prof.execution_policy} for i, prof, role in assignments],
        "workspace_policy": {"mode": "staging_copy"},
    }
    if images:
        payload["images"] = to_payload(images)
    try:
        result = EngineClient().submit(payload)
    except EngineError as exc:
        task.state = "engine_offline" if exc.status == 503 else "failed"
        task.error = str(exc)
        task.save(update_fields=["state", "error", "updated_at"])
        record(request=request, action="task.submit", project=project, target=str(task.id), outcome="error",
               error=str(exc))
        raise ApiError(str(exc), 503 if exc.status == 503 else 502, task=_task_dict(task))
    task.engine_task_id = result["id"]
    task.state = result.get("state", "queued")
    task.save(update_fields=["engine_task_id", "state", "updated_at"])
    record(request=request, action="task.submit", project=project, target=str(task.id),
           assignments=[f"{p.name}:{r}" for _, p, r in assignments], images=len(images))
    return _task_dict(task)


@api_view(["GET"])
def task_detail(request, task_id):
    task = _get_task(request.user, task_id)
    engine = service.sync_task(task) if task.state not in ("applied", "rejected", "engine_offline") else None
    task.refresh_from_db()
    d = _task_dict(task, events=request.GET.get("events") == "1", full=True)
    d["engine_online"] = engine is not None or not task.engine_task_id
    return d


@api_view(["POST"])
def task_cancel(request, task_id):
    task = _get_task(request.user, task_id)
    try:
        result = EngineClient().cancel(task.engine_task_id)
    except EngineError as exc:
        _engine_error(exc)
    record(request=request, action="task.cancel", project=task.project, target=str(task.id), outcome="cancelled")
    service.sync_task(task)
    return result


@api_view(["POST"])
def task_followup(request, task_id):
    """Continue one step's conversation (default: the latest) with a new message.
    The agent resumes in the same staging copy, so its edits add to the task's change set."""
    task = _get_task(request.user, task_id)
    data = json_body(request)
    message = str(data.get("message") or "").strip()
    if not message:
        raise ApiError("Write what the agent should do next")
    if len(message) > 20000:
        raise ApiError("message too long (max 20000 chars)")
    images = _request_images(data)
    read_only = data.get("read_only")
    if read_only is not None and not isinstance(read_only, bool):
        raise ApiError("read_only must be true, false or left out (same access as before)")
    service.sync_task(task)
    task.refresh_from_db()
    status = _follow_up_status(task)
    if not status["available"]:
        raise ApiError(status["reason"], 409)
    steps = task.assignments.select_related("profile")
    step = data.get("step")
    if step is None:
        src = steps.exclude(session_id="").order_by("-order").first()
    elif isinstance(step, int) and not isinstance(step, bool):
        src = steps.filter(order=step).first()
    else:
        raise ApiError("step must be a step number")
    if src is None:
        raise ApiError("Unknown step", 404)
    if not src.session_id:
        raise ApiError(f"Step {src.order + 1} ({src.role}) did not report a conversation, so it can't be continued",
                       409)
    payload = {"message": message, "resume_from": src.order, "checks": _checks_payload(task.project)}
    if src.profile:  # the profile's current settings (model, time limit, commands) apply to the follow-up
        if not src.profile.enabled:
            raise ApiError(f'Profile "{src.profile.name}" is turned off; turn it on to continue its conversation', 409)
        payload.update(policy=src.profile.execution_policy, profile=src.profile.name)
    if isinstance(read_only, bool):
        payload["read_only"] = read_only
    if images:
        payload["images"] = to_payload(images)
    try:
        result = EngineClient().followup(task.engine_task_id, payload)
    except EngineError as exc:
        _engine_error(exc)
    new = result["assignments"][-1]
    saved = save_images(task.attachments_dir, images, step_prefix(new["index"])) if images else []
    TaskAssignment.objects.update_or_create(
        task=task, order=new["index"],
        defaults={"profile": src.profile, "adapter_identifier": src.adapter_identifier, "role": src.role,
                  "attachments": saved, **service._assignment_fields(new)})
    task.state, task.error, task.finished_at = result.get("state", "queued"), "", None
    task.can_follow_up = bool(result.get("can_follow_up"))
    task.save(update_fields=["state", "error", "finished_at", "can_follow_up", "updated_at"])
    record(request=request, action="task.followup", project=task.project, target=str(task.id),
           step=src.order, read_only=new["policy"].get("read_only"), images=len(images))
    return _task_dict(task, full=True)


@api_view(["GET"])
def task_image(request, task_id, name):
    """One image the user attached to the task or a follow-up (only types sniffed from the bytes
    are ever stored, so the content type is safe to serve inline)."""
    task = _get_task(request.user, task_id)
    if not STORED_NAME.match(name):
        raise Http404("Image not found")
    records = list(task.attachments or []) + [r for a in task.assignments.all() for r in a.attachments or []]
    rec = next((r for r in records if r.get("file") == name), None)
    path = task.attachments_dir / name
    if rec is None or not path.is_file():
        raise Http404("Image not found")
    resp = FileResponse(open(path, "rb"), content_type=rec.get("media_type") or "application/octet-stream")
    resp["Content-Disposition"] = "inline"
    resp["Cache-Control"] = "private, max-age=86400"
    return resp


@api_view(["GET"])
def task_changes(request, task_id):
    task = _get_task(request.user, task_id)
    try:
        changes = service.fetch_changes(task)
    except EngineError as exc:
        _engine_error(exc)
    out = []
    for c in changes:
        st = service.live_status(task.project, c)
        out.append({k: c.get(k) for k in ("path", "action", "binary", "sensitive", "size", "diff", "base_text",
                                         "new_text", "too_large", "base_sha256", "new_sha256")} |
                   {"applicable": st["ok"], "conflict_reason": None if st["ok"] else st["reason"]})
    return {"task_id": str(task.id), "state": task.state, "changes": out}


@api_view(["POST"])
def task_approve(request, task_id):
    task = _get_task(request.user, task_id)
    data = json_body(request)
    if data.get("confirm") is not True:
        raise ApiError("Applying agent changes requires explicit confirmation (confirm: true)", 428)
    files = data.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ApiError("files must be a list of paths")
    service.sync_task(task)
    task.refresh_from_db()
    try:
        return service.apply_changes(task, files, request.user, request)
    except EngineError as exc:
        _engine_error(exc)


@api_view(["POST"])
def task_reject(request, task_id):
    task = _get_task(request.user, task_id)
    service.sync_task(task)
    task.refresh_from_db()
    if task.state != "awaiting_review":
        raise ApiError(f"Task is not awaiting review (state: {task.state})", 409)
    try:
        EngineClient().approve(task.engine_task_id, {"decision": "rejected", "files": [],
                                                     "actor": request.user.username})
    except EngineError as exc:
        _engine_error(exc)
    task.state, task.can_follow_up = "rejected", False
    task.save(update_fields=["state", "can_follow_up", "updated_at"])
    from .models import ChangeSet

    ChangeSet.objects.filter(task=task).update(approval_status="rejected", decided_by=request.user)
    record(request=request, action="agent.reject", project=task.project, target=str(task.id))
    return _task_dict(task)


@api_view(["GET"])
def audit_list(request):
    from audit.models import AuditEvent

    qs = AuditEvent.objects.select_related("actor", "project")
    if not request.user.is_staff:
        qs = qs.filter(actor=request.user)
    if request.GET.get("project"):
        qs = qs.filter(project_id=request.GET["project"])
    return {"events": [{"at": e.created_at.isoformat(), "actor": e.actor.username if e.actor else None,
                        "project": e.project.name if e.project else None, "action": e.action, "target": e.target,
                        "outcome": e.outcome, "detail": e.detail} for e in qs[:200]]}


# ---- SSE proxy (async; works under ASGI/daphne) ---------------------------

def _sse(event: str, data: dict, seq: int | None = None) -> str:
    """Default-'message' SSE frames (EventSource.onmessage); the event type
    travels inside the JSON body as ``type``."""
    head = f"id: {seq}\n" if seq else ""
    body = dict(data)
    body.setdefault("type", event)
    return f"{head}data: {json.dumps(body, default=str)}\n\n"


@require_GET
async def task_events(request, task_id):
    """Stream task events to the browser: stored history first, then live
    events relayed from the engine (persisted as they pass). If the engine is
    offline the stored history is replayed and the stream ends cleanly."""
    user = await request.auser()
    if not user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)
    try:
        task = await sync_to_async(_get_task)(user, task_id)
    except Http404:
        return JsonResponse({"error": "Task not found"}, status=404)
    try:
        after = int(request.GET.get("after") or request.headers.get("Last-Event-ID") or 0)
    except ValueError:
        after = 0

    def stored(after_seq):
        return [{"seq": e.sequence, "type": e.type, "payload": e.payload, "ts": e.timestamp.timestamp()}
                for e in TaskEvent.objects.filter(task=task, sequence__gt=after_seq).order_by("sequence")[:5000]]

    async def gen():
        last = after
        for ev in await sync_to_async(stored)(last):
            last = ev["seq"]
            yield _sse(ev["type"], ev, ev["seq"])
        if not task.engine_task_id or task.state in ("applied", "rejected", "engine_offline"):
            yield _sse("end", {"type": "end", "state": task.state, "engine": "not-contacted"})
            return
        try:
            async for item in EngineClient().stream_events(task.engine_task_id, after=last):
                if item["event"] == "keepalive":
                    yield ": keepalive\n\n"
                    continue
                if item["event"] == "end":
                    await sync_to_async(service.sync_task)(task)
                    yield _sse("end", {**item["data"], "type": "end"})
                    return
                ev = item["data"]
                await sync_to_async(service.store_event)(task, ev)
                last = ev.get("seq", last)
                yield _sse(ev.get("type", "message"), ev, ev.get("seq"))
        except EngineError as exc:
            yield _sse("engine_offline", {"type": "engine_offline", "error": str(exc)})
        except asyncio.CancelledError:
            raise

    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-store"
    resp["X-Accel-Buffering"] = "no"
    return resp

