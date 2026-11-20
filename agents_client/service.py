"""IDE-side task bookkeeping and the validated apply of agent change sets."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone as djtz

from audit.log import record
from core.api import ApiError, Conflict
from cs_common.redact import is_sensitive_path, redact_obj
from cs_common.redact import redact as redact_text
from files import snapshots
from files.fsutil import atomic_write, sha256_file
from projects.paths import PathSecurityError, project_root, resolve, split_relative

from .engine_client import EngineClient, EngineError
from .models import AgentAdapter, ChangeSet, Task, TaskAssignment, TaskEvent


def _ts(value) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return djtz.now()


def sync_adapters(agents: list[dict]) -> None:
    for a in agents:
        AgentAdapter.objects.update_or_create(
            identifier=a["identifier"],
            defaults={"display_name": a.get("display_name", "")[:128], "executable": (a.get("executable") or "")[:1024],
                      "capability_snapshot": {"capabilities": a.get("capabilities"), "notes": a.get("notes"),
                                              "auth_state": a.get("auth_state")},
                      "available": bool(a.get("available")), "version": (a.get("version") or "")[:128],
                      "last_probed_at": djtz.now()})


def store_event(task: Task, ev: dict) -> TaskEvent | None:
    """Persist one engine event (redacted + bounded). Idempotent on sequence."""
    seq = int(ev.get("seq") or 0)
    if seq <= 0:
        return None
    payload = redact_obj(ev.get("payload") or {})
    text = json.dumps(payload, default=str)
    if len(text) > TaskEvent.MAX_PAYLOAD_CHARS:
        payload = {"truncated": True, "preview": text[:TaskEvent.MAX_PAYLOAD_CHARS]}
    try:
        with transaction.atomic():
            obj = TaskEvent.objects.create(task=task, sequence=seq, type=str(ev.get("type", ""))[:64],
                                           payload=payload, timestamp=_ts(ev.get("ts")))
    except IntegrityError:
        return None
    if seq > task.last_event_seq:
        Task.objects.filter(pk=task.pk, last_event_seq__lt=seq).update(last_event_seq=seq)
        task.last_event_seq = seq
    if ev.get("type") == "task.state" and payload.get("state"):
        apply_engine_state(task, {"state": payload["state"], "error": payload.get("error")})
    return obj


def apply_engine_state(task: Task, data: dict) -> None:
    state = data.get("state")
    if not state or task.state in ("applied", "rejected"):
        return
    fields = []
    if state != task.state:
        task.state = state
        fields.append("state")
    if data.get("error"):
        task.error = str(data["error"])[:4000]
        fields.append("error")
    if state not in Task.TERMINAL and task.finished_at:  # a follow-up started the task again
        task.finished_at = None
        fields.append("finished_at")
    if state == "running" and not task.started_at:
        task.started_at = djtz.now()
        fields.append("started_at")
    if state in Task.TERMINAL and not task.finished_at:
        task.finished_at = djtz.now()
        fields.append("finished_at")
    if fields:
        task.save(update_fields=fields + ["updated_at"])


MAX_REPLY_CHARS = 60_000
_STAT_KEYS = ("num_turns", "duration_ms", "cost_usd", "is_error", "subtype", "checks_total", "checks_failed")


def _assignment_fields(a: dict) -> dict:
    """Engine assignment -> TaskAssignment columns (reply redacted again; the
    engine already redacts, but the IDE never trusts that alone)."""
    stats = a.get("stats") if isinstance(a.get("stats"), dict) else {}
    exit_code = a.get("exit_code")
    return {
        "status": str(a.get("status") or "pending")[:32],
        "result": redact_text(str(a.get("result") or ""))[:MAX_REPLY_CHARS],
        "error": redact_text(str(a.get("error") or ""))[:4000],
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "stats": {k: stats[k] for k in _STAT_KEYS if isinstance(stats.get(k), (int, float, str, bool))},
        "started_at": _ts(a["started_at"]) if a.get("started_at") else None,
        "finished_at": _ts(a["finished_at"]) if a.get("finished_at") else None,
        "read_only": bool((a.get("policy") or {}).get("read_only")),
        "session_id": str(a.get("session_id") or "")[:200],
        "followup_of": a["followup_of"] if isinstance(a.get("followup_of"), int) else None,
        "message": redact_text(str(a.get("message") or ""))[:20000],
    }


def sync_task(task: Task, client: EngineClient | None = None) -> dict | None:
    """Refresh state/assignments/events from the engine. Returns engine data or None if offline."""
    if not task.engine_task_id:
        return None
    client = client or EngineClient()
    try:
        data = client.task(task.engine_task_id)
        events = client.events(task.engine_task_id, after=task.last_event_seq)
    except EngineError:
        return None
    for ev in events.get("events", []):
        store_event(task, ev)
    apply_engine_state(task, data)
    for a in data.get("assignments", []):
        fields = _assignment_fields(a)
        if not task.assignments.filter(order=a.get("index")).update(**fields) and isinstance(a.get("index"), int):
            # a follow-up the IDE has not recorded yet (e.g. its response was lost)
            src = task.assignments.filter(order=a.get("followup_of")).first()
            TaskAssignment.objects.create(task=task, order=a["index"], profile=src.profile if src else None,
                                          adapter_identifier=str(a.get("adapter") or "")[:64],
                                          role=str(a.get("role") or "")[:64], **fields)
    can = bool(data.get("can_follow_up"))
    if can != task.can_follow_up:
        task.can_follow_up = can
        task.save(update_fields=["can_follow_up"])
    changes = data.get("changes_summary")
    if isinstance(changes, list) and changes != task.changes_summary:
        task.changes_summary = [{k: c.get(k) for k in ("path", "action", "binary", "sensitive", "size")}
                                for c in changes[:500] if isinstance(c, dict)]
        task.save(update_fields=["changes_summary"])
    ws = data.get("workspace") or {}
    if ws.get("ref") and task.workspace_ref != ws["ref"]:
        task.workspace_ref = ws["ref"]
        task.save(update_fields=["workspace_ref"])
    return data


def changeset_path(task: Task) -> Path:
    d = Path(settings.DATA_DIR) / "changesets"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task.id}.json"


def fetch_changes(task: Task, client: EngineClient | None = None) -> list[dict]:
    """Fetch proposed changes and persist them as a bounded file artifact."""
    client = client or EngineClient()
    data = client.changes(task.engine_task_id)
    changes = data.get("changes", [])
    path = changeset_path(task)
    path.write_text(json.dumps(changes), encoding="utf-8")
    summary = [{k: c.get(k) for k in ("path", "action", "binary", "sensitive", "size", "base_sha256", "new_sha256")}
               for c in changes]
    ChangeSet.objects.update_or_create(task=task, defaults={"artifact_path": str(path), "summary": summary,
                                                            "base_revision": data.get("base_revision") or ""})
    return changes


def live_status(project, change: dict) -> dict:
    """Compare the live file with the change's base hash (conflict detection)."""
    try:
        target = resolve(project, change["path"], allow_root=False)
    except PathSecurityError as exc:
        return {"ok": False, "reason": f"path rejected: {exc}"}
    if change.get("sensitive") or is_sensitive_path(change["path"]):
        return {"ok": False, "reason": "sensitive/credential file: agents may not write it"}
    if target.exists() and not target.is_file():
        return {"ok": False, "reason": "target is not a regular file"}
    live_sha = sha256_file(target) if target.is_file() else None
    action = change["action"]
    if action == "added":
        if live_sha is not None and live_sha != change.get("new_sha256"):
            return {"ok": False, "reason": "a different file now exists at this path", "live_sha256": live_sha}
    elif live_sha != change.get("base_sha256"):
        if action == "deleted" and live_sha is None:
            return {"ok": True, "noop": True}
        return {"ok": False, "reason": "file changed in the project since the task started", "live_sha256": live_sha}
    if action != "deleted" and "content_b64" not in change:
        return {"ok": False, "reason": "change too large to apply from the review panel"}
    return {"ok": True}


def apply_changes(task: Task, selected: list[str], user, request=None) -> dict:
    project = task.project
    if task.state != "awaiting_review":
        raise ApiError(f"Task is not awaiting review (state: {task.state})", 409)
    changes = {c["path"]: c for c in fetch_changes(task)}
    selected = list(dict.fromkeys(selected))  # de-duplicate, keep order
    if not selected:
        raise ApiError("Select at least one file to apply")
    unknown = [p for p in selected if p not in changes]
    if unknown:
        raise ApiError(f"Unknown paths in selection: {unknown[:5]}")
    project_root(project)  # re-validate root
    conflicts = []
    for rel in selected:
        split_relative(rel)
        st = live_status(project, changes[rel])
        if not st["ok"]:
            conflicts.append({"path": rel, "reason": st["reason"]})
    if conflicts:
        ChangeSet.objects.filter(task=task).update(approval_status="conflict")
        record(actor=user, request=request, action="agent.apply", project=project, target=str(task.id),
               outcome="conflict", conflicts=conflicts[:20])
        raise Conflict("Some changes conflict with the live project; nothing was applied", conflicts=conflicts)

    sid = snapshots.create_snapshot(project, "agent-apply", selected, user, note=f"before applying task {task.id}")
    applied = []
    try:
        for rel in selected:
            change = changes[rel]
            target = resolve(project, rel, allow_root=False)
            if change["action"] == "deleted":
                if target.exists():
                    target.unlink()
            else:
                data = base64.b64decode(change["content_b64"])
                if hashlib.sha256(data).hexdigest() != change.get("new_sha256"):
                    raise ApiError(f"Integrity check failed for {rel}", 500)
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, data)
            applied.append(rel)
    except Exception as exc:
        snapshots.rollback_to(project, sid)
        ChangeSet.objects.filter(task=task).update(approval_status="failed")
        record(actor=user, request=request, action="agent.apply", project=project, target=str(task.id),
               outcome="error", error=str(exc), rolled_back_to=sid)
        raise ApiError(f"Apply failed and was rolled back: {exc}", 500, snapshot=sid)

    decision = "applied" if len(applied) == len(changes) else "partially_applied"
    ChangeSet.objects.filter(task=task).update(approval_status=decision, decided_by=user, decided_at=djtz.now(),
                                               snapshot_id=sid)
    task.state, task.can_follow_up = "applied", False  # the engine drops the staging copy
    task.save(update_fields=["state", "can_follow_up", "updated_at"])
    record(actor=user, request=request, action="agent.apply", project=project, target=str(task.id),
           files=applied[:200], snapshot=sid, decision=decision)
    engine_ack = True
    try:
        EngineClient().approve(task.engine_task_id, {"decision": decision, "files": applied, "actor": user.username})
    except EngineError:
        engine_ack = False  # applied locally; engine will be told on next sync
    return {"applied": applied, "snapshot": sid, "decision": decision, "engine_acknowledged": engine_ack}
