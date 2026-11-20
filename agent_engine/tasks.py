"""Task orchestration: staging workspace -> sequential role assignments -> diff.

Task states: queued -> preparing -> running -> awaiting_review | completed |
failed | cancelled; after review: applied | rejected. ``interrupted`` marks
tasks that were in flight when the engine restarted.

Follow-ups: a finished task keeps its staging copy (until it is applied,
rejected or expires), and every assignment records the conversation id its
CLI reported. ``followup()`` appends a new assignment that resumes one of
those conversations in the same staging copy, so edits accumulate and are
reviewed together.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cs_common.agent_policy import PolicyError, clean_allowed_commands, clean_check_env, clean_excludes
from cs_common.images import ImageError, clean_images, save_images, step_prefix
from cs_common.redact import Redactor

from .adapters import AdapterUnavailable, BaseAdapter
from .adapters.base import SESSION_KEYS
from .config import EngineConfig
from .workspace import HANDOFF_DIR, WorkspaceError, compute_changes, create_workspace, remove_workspace

log = logging.getLogger("agent_engine")

TERMINAL = {"awaiting_review", "completed", "failed", "cancelled", "applied", "rejected", "interrupted"}
MAX_EVENTS_IN_MEMORY = 5000
MAX_OBJECTIVE = 20_000
MAX_RESULT = 60_000  # each agent's final reply, shown in the task summary
MAX_ASSIGNMENTS = 50  # the first lineup (max 10) plus follow-ups
# finished states a task can be followed up from (applied/rejected discard the staging copy)
FOLLOWUP_STATES = {"awaiting_review", "completed", "failed", "cancelled", "interrupted"}

ROLE_GUIDANCE = {
    "architect": (
        "Study the codebase and write an implementation plan that another agent can follow without "
        "re-investigating. Before planning, read the code the objective touches and the code that depends on it. "
        "Cover: what the code does today (with file:line references); the files and functions to change, and why; "
        "the steps in order; edge cases, risks and security concerns; how to test it; and what is out of scope. "
        "Put the whole plan in your final reply, because later steps only receive that reply. "
        "Do not modify files unless asked."),
    "implementer": "Implement the objective by editing files in the current working directory.",
    "reviewer": (
        "Review the current state of the workspace against the objective and the plan, and report issues. "
        "Do not modify files. Number each finding and give its severity (critical, high, medium or low), the file "
        "and line, the cause, and the fix. End with one line: 'VERDICT: APPROVE' or 'VERDICT: CHANGES REQUESTED'."),
    "tester": "Add or update tests that verify the objective, then report what you covered.",
}
REVIEW_ROLE_KEYS = ("review", "critic", "audit")


MAX_PROMPT_DIFF = 30_000
CHECK_REPORT_IN_PROMPT = 12_000  # a Verify step's report reaches later steps nearly whole (failures are at the end)
# How much of an earlier step's reply later steps see in their prompt, by the job it did (matched in the
# role name). Every reply is also in the handoff folder in full; the prompt carries the parts most acted on.
REPLY_LIMITS = (("architect", 20_000), ("plan", 20_000), ("design", 20_000),
                ("review", 8_000), ("critic", 8_000), ("audit", 8_000))
DEFAULT_REPLY_LIMIT = 4_000
EARLIER_REPLIES_BUDGET = 45_000  # all earlier replies together; shared fairly when they exceed it

HANDOFF_NOTE = (
    f"The full, untrimmed output of every earlier step is in the {HANDOFF_DIR}/ folder: task.json lists the steps, "
    "each step-NN-<role>.md holds one step's complete reply, and changes.diff holds every change made so far. "
    "The summaries in this prompt may be shortened, so read those files in full for anything you act on. "
    "Code Studio maintains that folder: do not edit it. It is never part of your changes.")
ATTACHMENTS_DIR = "attachments"  # inside the handoff folder: images the user attached


def image_note(images: list[dict] | None, attached: bool) -> str:
    """Tells the agent about the user's images (copied into the handoff folder)."""
    if not images:
        return ""
    n = len(images)
    lines = [f"The user attached {n} image{'s' if n > 1 else ''} to this request (screenshots, mockups or "
             "diagrams that are part of the instructions):"]
    lines += [f"- {HANDOFF_DIR}/{ATTACHMENTS_DIR}/{im['file']} (\"{im['name']}\")" for im in images]
    if attached:
        lines.append("They are also attached to this message. Look at every image before you start.")
    else:
        lines.append("Look at every image before you start: open each file with your file-reading tool, which "
                     "can display images. If you cannot view images, say so in your reply instead of guessing "
                     "what they show.")
    return "\n".join(lines)


def is_review_role(role) -> bool:
    role = str(role or "").lower()
    return any(k in role for k in REVIEW_ROLE_KEYS)


def reply_limit(a: dict) -> int:
    if a["adapter"] == "verify":
        return CHECK_REPORT_IN_PROMPT
    role = str(a.get("role") or "").lower()
    return next((n for key, n in REPLY_LIMITS if key in role), DEFAULT_REPLY_LIMIT)


def step_file(a: dict) -> str | None:
    """The step's file name in the handoff folder, e.g. ``step-02-implementer.md``."""
    if not isinstance(a.get("index"), int):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(a.get("role") or "").lower()).strip("-")[:40] or "step"
    return f"step-{a['index'] + 1:02d}-{slug}.md"


def step_summary(a: dict, status: str) -> dict:
    """What later steps are told about an earlier step."""
    return {"role": a["role"], "adapter": a["adapter"], "status": status,
            "summary": a.get("result") or a.get("error") or "", "limit": reply_limit(a), "file": step_file(a)}


def step_markdown(a: dict) -> str:
    """One step's full record, as written to the handoff folder."""
    agent = a["adapter"] + (f" ({a['profile']})" if a.get("profile") and a["profile"] != a["adapter"] else "")
    lines = [f"# Step {a['index'] + 1}: {a['role']}", "", f"- Agent: {agent}", f"- Status: {a['status']}"]
    if (a.get("policy") or {}).get("read_only"):
        lines.append("- Read-only: yes")
    if a.get("followup_of") is not None:
        lines.append(f"- Follow-up to step {a['followup_of'] + 1}")
    if a.get("message"):
        lines += ["", "## Follow-up message from the user", "", a["message"]]
    if a.get("attachments"):
        lines += ["", "Attached images:"]
        lines += [f"- {ATTACHMENTS_DIR}/{im['file']} (\"{im['name']}\")" for im in a["attachments"]]
    lines += ["", "## Reply", "", a.get("result") or "(no reply)"]
    if a.get("error") and a.get("status") != "succeeded":
        lines += ["", "## Error", "", str(a["error"])]
    return "\n".join(lines) + "\n"


def write_handoff(work: Path, data: dict, changes: list[dict] | None, current: int | None = None,
                  attachments_src: Path | None = None) -> None:
    """(Re)write the task's handoff folder in the staging copy from the engine's own record, so whatever
    an agent did to it, the next step reads the truth. ``attachments_src``: the engine's copy of the
    user's images, copied into ``attachments/``."""
    root = work / HANDOFF_DIR
    remove_workspace(root)
    root.mkdir(parents=True, exist_ok=True)
    if attachments_src is not None and attachments_src.is_dir():
        shutil.copytree(attachments_src, root / ATTACHMENTS_DIR)
    steps = []
    for a in data.get("assignments") or []:
        name = step_file(a) if (a.get("result") or a.get("error")) and a["status"] != "pending" else None
        if name:
            (root / name).write_text(step_markdown(a), encoding="utf-8")
        steps.append({"step": a["index"] + 1, "role": a["role"], "agent": a["adapter"], "profile": a.get("profile"),
                      "status": a["status"], "read_only": bool((a.get("policy") or {}).get("read_only")),
                      "followup_of": a["followup_of"] + 1 if a.get("followup_of") is not None else None,
                      "file": name, "current": a["index"] == current,
                      "attachments": [f"{ATTACHMENTS_DIR}/{im['file']}" for im in a.get("attachments") or []]})
    diffs = [c["diff"] for c in changes or [] if c.get("diff") and not c.get("sensitive") and not c.get("binary")]
    notes = [f"# {c['path']}: {'sensitive file, diff not shown' if c.get('sensitive') else 'binary file changed'}\n"
             for c in changes or [] if c.get("sensitive") or c.get("binary")]
    if diffs or notes:
        (root / "changes.diff").write_text("".join(notes + diffs), encoding="utf-8")
    index = {"objective": data.get("objective"), "steps": steps,
             "attachments": [{"file": f"{ATTACHMENTS_DIR}/{im['file']}", "name": im["name"]}
                             for im in data.get("attachments") or []],
             "changes_file": "changes.diff" if (diffs or notes) else None,
             "changed_files": [{"path": c["path"], "action": c["action"]} for c in changes or []]}
    (root / "task.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    (root / "README.md").write_text(
        "# Code Studio task handoff\n\nWritten by Code Studio before each step of this task. `task.json` lists "
        "the steps (in order) and the files changed so far; each `step-NN-<role>.md` is one step's complete "
        "reply; `changes.diff` has every diff against the original project; `attachments/` holds images the "
        "user attached to the task or a follow-up.\n\nDo not edit this folder: it is "
        "rewritten before every step and is never part of the task's changes.\n", encoding="utf-8")


def fit_replies(items: list[dict], budget: int) -> list[str]:
    """Each earlier reply cut to its own limit; if together they exceed ``budget``, one common
    cap is found so short replies stay whole and the long ones share what is left. A cut reply
    keeps its start and its end (where agents put caveats) and says where the full text is."""
    wants = [min(len(p["summary"]), p.get("limit", DEFAULT_REPLY_LIMIT)) for p in items]
    if sum(wants) > budget:
        lo, hi = 0, max(wants)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if sum(min(w, mid) for w in wants) <= budget:
                lo = mid
            else:
                hi = mid - 1
        wants = [min(w, lo) for w in wants]
    out = []
    for p, n in zip(items, wants):
        text = p["summary"]
        if n >= len(text):
            out.append(text)
            continue
        where = f"; the full reply is in {HANDOFF_DIR}/{p['file']}" if p.get("file") else ""
        marker = f"\n[... {len(text) - n} characters not shown here{where} ...]\n"
        head = (n * 2) // 3
        tail = n - head
        out.append(text[:head].rstrip() + marker + (text[-tail:].lstrip() if tail else ""))
    return out


def _earlier_lines(items: list[dict], budget: int) -> list[str]:
    return [f"- [{p['role']} via {p['adapter']}] status={p['status']}: {text}"
            for p, text in zip(items, fit_replies(items, budget))]


def describe_changes(changes: list[dict], diff_budget: int = MAX_PROMPT_DIFF) -> str:
    """What earlier steps changed, as a bounded file list + unified diffs
    (the staging copy has no .git, so agents cannot run git diff)."""
    if not changes:
        return "Earlier steps in this task have not changed any files yet."
    lines = [f"Files changed so far by earlier steps ({len(changes)}):"]
    lines += [f"- {c['action']}: {c['path']}" for c in changes[:200]]
    if len(changes) > 200:
        lines.append(f"- ... and {len(changes) - 200} more")
    budget, diffs = diff_budget, []
    for c in changes:
        if c.get("sensitive") or c.get("binary") or not c.get("diff"):
            continue
        if len(c["diff"]) > budget:
            diffs.append(f"[diff of {c['path']} omitted: prompt size limit; see {HANDOFF_DIR}/changes.diff]")
            continue
        budget -= len(c["diff"])
        diffs.append(c["diff"].rstrip("\n"))
    if diffs:
        lines.append("\nUnified diffs against the original project:\n" + "\n".join(diffs))
    return "\n".join(lines)


def review_note(role: str, previous: list[dict], read_only: bool) -> str:
    """An editing step after a review is told to act on the findings and account for each one."""
    if read_only or is_review_role(role):
        return ""
    review = next((p for p in reversed(previous) if is_review_role(p["role"]) and p["status"] == "succeeded"), None)
    if review is None:
        return ""
    where = f" (full findings: {HANDOFF_DIR}/{review['file']})" if review.get("file") else ""
    return (f"A {review['role']} step reviewed the work before you{where}. Fix every issue it raised unless you "
            "have a good reason not to. End your reply with a 'Review findings' section that lists each finding as "
            "fixed or not fixed, with the reason.")


def build_prompt(role: str, objective: str, previous: list[dict], read_only: bool, changes_so_far: str | None = None,
                 agent_notes: str = "", replies_budget: int = EARLIER_REPLIES_BUDGET, images: str = "") -> str:
    guidance = ROLE_GUIDANCE.get(role.lower(), f"Act as the '{role}' for this task.")
    lines = [
        f"You are acting as the {role} on a software task inside Code Studio.",
        guidance,
        "Work only inside the current working directory (an isolated staging copy of the project). "
        "Your changes will be reviewed by a human before they are applied.",
        "The staging copy does not contain the .git directory, so git commands (status, diff, log) will not work. "
        "Inspect files directly; the changes made by earlier steps are listed below when there are any.",
        "Refer to files by their path relative to the project root (for example src/app.py:42), never by "
        "absolute path: the staging copy is deleted after the task.",
    ]
    if agent_notes:
        lines.append(agent_notes)
    if read_only:
        lines.append("This assignment is READ-ONLY: do not create, edit or delete files.")
    if previous:
        lines.append(HANDOFF_NOTE)
        note = review_note(role, previous, read_only)
        if note:
            lines.append(note)
        lines.append("\nOutput from earlier assignments in this task:")
        lines += _earlier_lines(previous, replies_budget)
    if changes_so_far:
        lines.append("\n" + changes_so_far)
    lines.append("\nObjective:\n" + objective)
    if images:
        lines.append("\n" + images)
    return "\n".join(lines)


def build_followup_prompt(message: str, read_only: bool, was_read_only: bool, since: list[dict],
                          changes_so_far: str | None = None, agent_notes: str = "",
                          replies_budget: int = EARLIER_REPLIES_BUDGET, images: str = "") -> str:
    """Prompt for a resumed conversation: the agent already has the objective and
    its own earlier turns, so only what is new is sent."""
    lines = ["The user is following up on your earlier work in this conversation. You are in the same staging "
             "copy of the project as before: your earlier edits are still there and have not been applied yet.",
             HANDOFF_NOTE.replace("every earlier step", "every step of this task so far")]
    if agent_notes:
        lines.append(agent_notes)
    if read_only:
        lines.append("This follow-up is READ-ONLY: do not create, edit or delete files.")
    elif was_read_only:
        lines.append("Your earlier turn was read-only; for this follow-up you may edit files in the current "
                     "working directory.")
    if since:
        lines.append("\nOther steps ran after your last turn:")
        lines += _earlier_lines(since, replies_budget)
        if changes_so_far:
            lines.append("\n" + changes_so_far)
    lines.append("\nFollow-up from the user:\n" + message)
    if images:
        lines.append("\n" + images)
    return "\n".join(lines)


class EngineTask:
    def __init__(self, data: dict, task_dir: Path):
        self.data = data
        self.dir = task_dir
        self.events: list[dict] = []
        self.seq = 0
        self.cond = threading.Condition()
        self.cancel_event = threading.Event()
        self.current: tuple[BaseAdapter, str] | None = None
        self.lock = threading.RLock()

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def state(self) -> str:
        return self.data["state"]

    def public(self) -> dict:
        d = {k: v for k, v in self.data.items() if k not in ("manifest",)}
        d["event_count"] = self.seq
        d["can_follow_up"] = self.state in FOLLOWUP_STATES and bool(self.data.get("workspace_kept"))
        return d


class TaskManager:
    def __init__(self, config: EngineConfig, adapters: dict[str, BaseAdapter]):
        self.config = config
        self.adapters = adapters
        self.tasks: dict[str, EngineTask] = {}
        self.pool = ThreadPoolExecutor(max_workers=max(1, config.max_tasks), thread_name_prefix="engine-task")
        self.adapter_slots = {k: threading.BoundedSemaphore(1) for k in adapters}
        self.redactor = Redactor()
        self._load_existing()

    # ---- persistence ----------------------------------------------------
    def _save(self, task: EngineTask):
        with task.lock:
            tmp = task.dir / "task.json.tmp"
            tmp.write_text(json.dumps(task.data, default=str), encoding="utf-8")
            os.replace(tmp, task.dir / "task.json")

    def _load_existing(self):
        for tdir in sorted(self.config.tasks_dir.iterdir()) if self.config.tasks_dir.exists() else []:
            f = tdir / "task.json"
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            task = EngineTask(data, tdir)
            ev_file = tdir / "events.jsonl"
            if ev_file.is_file():
                for line in ev_file.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_EVENTS_IN_MEMORY:]:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    task.events.append(ev)
                    task.seq = max(task.seq, ev.get("seq", 0))
            if data.get("state") not in TERMINAL:
                data["state"] = "interrupted"
                data["error"] = "Engine restarted while the task was running"
                self._save(task)
            if "workspace_kept" not in data:  # tasks from before follow-ups existed
                data["workspace_kept"] = data.get("state") in FOLLOWUP_STATES and bool(data.get("manifest")) \
                    and (self.config.workspaces_dir / task.id / "work").is_dir()
                self._save(task)
            self.tasks[task.id] = task
        self.prune_workspaces()

    # ---- events ---------------------------------------------------------
    def emit(self, task: EngineTask, etype: str, payload: dict | None = None):
        payload = self.redactor.redact_obj(payload or {})
        with task.cond:
            task.seq += 1
            ev = {"seq": task.seq, "type": etype, "ts": time.time(), "payload": payload}
            task.events.append(ev)
            if len(task.events) > MAX_EVENTS_IN_MEMORY:
                task.events = task.events[-MAX_EVENTS_IN_MEMORY:]
            with open(task.dir / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, default=str) + "\n")
            task.cond.notify_all()

    def events_after(self, task: EngineTask, after: int) -> list[dict]:
        with task.cond:
            return [e for e in task.events if e["seq"] > after]

    def _set_state(self, task: EngineTask, state: str, **extra):
        with task.lock:
            task.data["state"] = state
            task.data["updated_at"] = time.time()
            task.data.update(extra)
            self._save(task)
        self.emit(task, "task.state", {"state": state, **{k: v for k, v in extra.items() if k != "changes"}})

    # ---- API operations ----------------------------------------------------
    def submit(self, payload: dict) -> EngineTask:
        objective = str(payload.get("objective") or "").strip()
        if not objective:
            raise ValueError("objective is required")
        if len(objective) > MAX_OBJECTIVE:
            raise ValueError("objective too long")
        project = payload.get("project") or {}
        source = self._validate_source(project.get("root"))
        assignments = payload.get("assignments") or []
        if not isinstance(assignments, list) or not assignments:
            raise ValueError("at least one assignment is required")
        checks = self._clean_checks(project.get("checks"), source)
        try:
            images = clean_images(payload.get("images"))
        except ImageError as exc:
            raise ValueError(str(exc))
        clean_assignments = []
        for i, a in enumerate(assignments[:10]):
            adapter_id = a.get("adapter")
            if adapter_id not in self.adapters:
                raise ValueError(f"unknown adapter '{adapter_id}'")
            policy = a.get("policy") or {}
            timeout = min(max(int(policy.get("timeout_seconds") or 900), 5), 3600)
            try:
                allowed = [] if policy.get("read_only") else clean_allowed_commands(policy.get("allowed_commands"))
            except PolicyError as exc:
                raise ValueError(str(exc))
            clean_assignments.append({
                "index": i, "assignment_id": a.get("assignment_id"), "profile": str(a.get("profile") or adapter_id),
                "adapter": adapter_id, "role": str(a.get("role") or "implementer")[:64],
                "policy": {"model": policy.get("model") or None, "read_only": bool(policy.get("read_only")),
                           "timeout_seconds": timeout, "max_turns": policy.get("max_turns"),
                           "allowed_commands": allowed},
                "status": "pending", "result": None, "error": None, "stats": {},
                "started_at": None, "finished_at": None,
            })
        if not checks["verify_commands"] and any(getattr(self.adapters[a["adapter"]], "runs_checks", False)
                                                 for a in clean_assignments):
            raise ValueError("this project has no check commands for its Verify step "
                             "(set them in Settings > Project checks)")
        task_id = payload.get("task_id") or uuid.uuid4().hex
        if not isinstance(task_id, str) or not task_id.replace("-", "").isalnum() or len(task_id) > 64:
            raise ValueError("invalid task_id")
        if task_id in self.tasks:
            raise ValueError("task already exists")
        tdir = self.config.tasks_dir / task_id
        tdir.mkdir(parents=True)
        attachments = save_images(tdir / ATTACHMENTS_DIR, images, "task")
        now = time.time()
        data = {
            "id": task_id, "state": "queued", "objective": objective, "created_at": now, "updated_at": now,
            "project": {"id": project.get("id"), "name": project.get("name"), "git_mode": project.get("git_mode")},
            "source_root": str(source), "base_revision": project.get("base_revision"),
            "workspace_policy": {"mode": "staging_copy"}, "assignments": clean_assignments,
            "requested_by": payload.get("requested_by"), "error": None, "changes_summary": None,
            "workspace_kept": False, "checks": checks, "attachments": attachments,
        }
        task = EngineTask(data, tdir)
        self.tasks[task_id] = task
        self._save(task)
        self.emit(task, "task.submitted", {"objective": objective[:500], "assignments": len(clean_assignments),
                                           "images": len(attachments)})
        self.prune_workspaces()
        self.pool.submit(self._run, task)
        return task

    def followup(self, task: EngineTask, payload: dict) -> EngineTask:
        """Resume one assignment's conversation with a new message, in the task's
        kept staging copy. Raises ValueError (bad input) or LookupError (not possible now)."""
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        if len(message) > MAX_OBJECTIVE:
            raise ValueError("message too long")
        try:
            images = clean_images(payload.get("images"))
        except ImageError as exc:
            raise ValueError(str(exc))
        with task.lock:
            if task.state not in FOLLOWUP_STATES:
                raise LookupError(f"task cannot be followed up (state={task.state})")
            root = self.config.workspaces_dir / task.id
            if not task.data.get("workspace_kept") or not (root / "work").is_dir() or not task.data.get("manifest"):
                raise LookupError("the task's staging copy is gone (applied, rejected or expired)")
            assignments = task.data["assignments"]
            if len(assignments) >= MAX_ASSIGNMENTS:
                raise LookupError(f"a task can have at most {MAX_ASSIGNMENTS} steps; start a new task")
            idx = payload.get("resume_from")
            if idx is None:  # default: the latest step that reported a conversation
                idx = next((a["index"] for a in reversed(assignments) if a.get("session_id")), None)
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(assignments):
                raise LookupError("no step of this task reported a conversation to continue")
            src = assignments[idx]
            adapter = self.adapters.get(src["adapter"])
            if adapter is None or not adapter.supports_resume:
                raise LookupError(f"{src['adapter']} cannot continue a conversation")
            if not src.get("session_id"):
                raise LookupError(f"step {idx + 1} ({src['role']}) did not report a conversation id, so it cannot "
                                  "be continued")
            policy = dict(src["policy"])
            if isinstance(payload.get("policy"), dict):  # the IDE re-sends the profile's current policy
                p = payload["policy"]
                try:
                    allowed = [] if p.get("read_only") else clean_allowed_commands(p.get("allowed_commands"))
                except PolicyError as exc:
                    raise ValueError(str(exc))
                policy = {"model": p.get("model") or None, "read_only": bool(p.get("read_only")),
                          "timeout_seconds": min(max(int(p.get("timeout_seconds") or 900), 5), 3600),
                          "max_turns": p.get("max_turns"), "allowed_commands": allowed}
            if isinstance(payload.get("checks"), dict):  # the project's current settings (exclusions stay as copied)
                fresh = self._clean_checks(payload["checks"], Path(task.data["source_root"]))
                fresh["exclude"] = (task.data.get("checks") or {}).get("exclude", [])
                task.data["checks"] = fresh
            if isinstance(payload.get("read_only"), bool):
                policy["read_only"] = payload["read_only"]
                if policy["read_only"]:
                    policy["allowed_commands"] = []
            attachments = save_images(task.dir / ATTACHMENTS_DIR, images, step_prefix(len(assignments)))
            new = {
                "index": len(assignments), "assignment_id": payload.get("assignment_id"),
                "profile": str(payload.get("profile") or src["profile"]), "adapter": src["adapter"],
                "role": src["role"], "policy": policy, "followup_of": idx, "message": message,
                "resume_session": src["session_id"], "was_read_only": bool(src["policy"].get("read_only")),
                "status": "pending", "result": None, "error": None, "stats": {},
                "started_at": None, "finished_at": None, "attachments": attachments,
            }
            assignments.append(new)
            task.cancel_event = threading.Event()
            self._set_state(task, "queued", error=None, finished_at=None)
        self.emit(task, "task.followup", {"index": new["index"], "followup_of": idx, "role": new["role"],
                                          "adapter": new["adapter"], "message": message[:500],
                                          "images": len(attachments)})
        self.pool.submit(self._continue, task, [new])
        return task

    def prune_workspaces(self):
        """Drop kept staging copies of finished tasks idle longer than the retention period."""
        cutoff = time.time() - self.config.workspace_retention_days * 86400
        for task in list(self.tasks.values()):
            if task.data.get("workspace_kept") and task.state in FOLLOWUP_STATES \
                    and (task.data.get("updated_at") or 0) < cutoff:
                self._discard_workspace(task)

    def _discard_workspace(self, task: EngineTask):
        remove_workspace(self.config.workspaces_dir / task.id)
        with task.lock:
            task.data["workspace_kept"] = False
            self._save(task)

    def _clean_checks(self, raw, source: Path) -> dict:
        """The project's check settings (resolved by the IDE), validated again here."""
        raw = raw if isinstance(raw, dict) else {}
        try:
            out = {"verify_commands": clean_allowed_commands(raw.get("verify_commands")),
                   "env": clean_check_env(raw.get("env")), "exclude": clean_excludes(raw.get("exclude")),
                   "python_env": None, "python_env_bin": None}
        except PolicyError as exc:
            raise ValueError(f"project checks: {exc}")
        venv = raw.get("python_env")
        if venv:
            if not isinstance(venv, str) or not os.path.isabs(venv):
                raise ValueError("project checks: python_env must be an absolute path resolved by the IDE")
            real = Path(os.path.realpath(venv))
            src = os.path.normcase(str(source)).rstrip("\\/")
            if not os.path.normcase(str(real)).startswith(src + os.sep):
                raise ValueError("project checks: the Python environment must be inside the project")
            if not (real / "pyvenv.cfg").is_file():
                raise ValueError(f"project checks: {real.name} is not a Python virtual environment (no pyvenv.cfg)")
            out["python_env"] = str(real)
            bin_dir = real / ("Scripts" if os.name == "nt" else "bin")
            # a venv made on another OS (e.g. a Windows .venv seen from the Linux container) has no usable bin
            out["python_env_bin"] = str(bin_dir) if bin_dir.is_dir() else None
        return out

    def _validate_source(self, root) -> Path:
        if not root or not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError("project.root must be an absolute path resolved by the IDE")
        resolved = Path(os.path.realpath(root))
        if not resolved.is_dir():
            raise ValueError("project root does not exist")
        def within(c, p):
            c, p = os.path.normcase(str(c)), os.path.normcase(str(p)).rstrip("\\/")
            return c == p or c.startswith(p + os.sep)
        if within(resolved, self.config.data_dir) or within(self.config.data_dir, resolved):
            raise ValueError("project root overlaps the engine data directory")
        if self.config.allowed_roots and not any(within(resolved, r) for r in self.config.allowed_roots):
            raise ValueError("project root is outside the engine's allowed roots")
        if Path(resolved.anchor) == resolved:
            raise ValueError("refusing to stage a filesystem root")
        return resolved

    def cancel(self, task: EngineTask) -> dict:
        if task.state in TERMINAL:
            return {"cancelled": False, "state": task.state}
        task.cancel_event.set()
        cur = task.current
        if cur:
            try:
                cur[0].cancel(cur[1])
            except KeyError:
                pass
        self.emit(task, "task.cancel_requested", {})
        return {"cancelled": True}

    def changes(self, task: EngineTask) -> list[dict]:
        f = task.dir / "changes.json"
        if not f.is_file():
            return []
        return json.loads(f.read_text(encoding="utf-8"))

    def decide(self, task: EngineTask, decision: str, files: list[str], actor: str | None) -> dict:
        if task.state != "awaiting_review":
            raise ValueError(f"task is not awaiting review (state={task.state})")
        if decision not in ("applied", "rejected", "partially_applied"):
            raise ValueError("decision must be applied, partially_applied or rejected")
        state = "rejected" if decision == "rejected" else "applied"
        self._set_state(task, state, decision={"decision": decision, "files": files[:5000], "actor": actor,
                                               "at": time.time()})
        self._discard_workspace(task)
        return task.public()

    # ---- worker ---------------------------------------------------------
    def _run(self, task: EngineTask):
        ws_root = self.config.workspaces_dir / task.id
        try:
            self._set_state(task, "preparing", started_at=time.time())
            try:
                ws = create_workspace(Path(task.data["source_root"]), ws_root, self.config.max_workspace_bytes,
                                      self.config.max_workspace_files,
                                      exclude=(task.data.get("checks") or {}).get("exclude"))
            except WorkspaceError:
                remove_workspace(ws_root)  # a partial copy is of no use
                raise
            task.data["manifest"] = ws["manifest"]
            task.data["workspace"] = {"mode": "staging_copy", "ref": task.id, "files": len(ws["manifest"]),
                                      "bytes": ws["bytes"], "excluded_by_project": ws.get("excluded", 0)}
            task.data["workspace_kept"] = True  # kept after the run so the task can be followed up
            self.emit(task, "workspace.ready", task.data["workspace"])
            self._execute(task, task.data["assignments"], ws)
        except WorkspaceError as exc:
            self._set_state(task, "failed", finished_at=time.time(), error=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("task %s crashed", task.id)
            self._set_state(task, "failed", finished_at=time.time(), error=f"Engine error: {exc}")

    def _continue(self, task: EngineTask, run: list[dict]):
        """Run follow-up assignments in the task's kept staging copy."""
        root = self.config.workspaces_dir / task.id
        ws = {"base": str(root / "base"), "work": str(root / "work"), "manifest": task.data.get("manifest") or {}}
        try:
            self._execute(task, run, ws)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("task %s follow-up crashed", task.id)
            self._set_state(task, "failed", finished_at=time.time(), error=f"Engine error: {exc}")

    def _execute(self, task: EngineTask, run: list[dict], ws: dict):
        """Run ``run`` (assignments of this task) in order, then compute the change set."""
        self._set_state(task, "running")
        work = Path(ws["work"])
        previous = []
        failed = False
        for a in run:
            if task.cancel_event.is_set():
                a["status"] = "cancelled"
                continue
            # nothing has run yet in a fresh task, so there is nothing to diff
            ran = any(b["status"] not in ("pending", "skipped") for b in task.data["assignments"] if b is not a)
            changes = compute_changes(Path(ws["base"]), work, ws["manifest"]) if ran else []
            self._handoff(task, work, changes, current=a["index"])
            prompt = self._prompt_for(task, a, previous, ws, work, changes=changes)
            status = self._run_assignment(task, a, work, prompt)
            previous.append(step_summary(a, status))
            self._save(task)
            if status != "succeeded":
                failed = True
                break
        for a in run:
            if a["status"] == "pending":
                a["status"] = "skipped"
        changes = compute_changes(Path(ws["base"]), work, ws["manifest"])
        self._handoff(task, work, changes)  # final record, for follow-ups
        (task.dir / "changes.json").write_text(json.dumps(changes), encoding="utf-8")
        summary = [{k: c[k] for k in ("path", "action", "binary", "sensitive", "size")} for c in changes]
        self.emit(task, "changes.ready", {"count": len(changes), "files": summary[:200]})
        task.data["changes_summary"] = summary
        if task.cancel_event.is_set():
            self._set_state(task, "cancelled", finished_at=time.time(), error="Cancelled by user")
        elif failed:
            failed_a = next(a for a in run if a["status"] not in ("succeeded", "skipped"))
            self._set_state(task, "failed", finished_at=time.time(),
                            error=f"{failed_a['role']} ({failed_a['adapter']}): {failed_a.get('error')}")
        else:
            self._set_state(task, "awaiting_review" if changes else "completed", finished_at=time.time())

    def _handoff(self, task: EngineTask, work: Path, changes: list[dict], current: int | None = None):
        """Write the handoff folder; a failure is reported but never stops the task."""
        try:
            write_handoff(work, task.data, changes, current, attachments_src=task.dir / ATTACHMENTS_DIR)
        except OSError as exc:
            log.warning("task %s: could not write the handoff folder: %s", task.id, exc)
            self.emit(task, "agent.stderr", {"assignment": current, "text": (
                f"Could not write the {HANDOFF_DIR} folder ({exc}); this step only gets the summaries in its prompt.")})

    def _prompt_for(self, task: EngineTask, a: dict, previous: list[dict], ws: dict, work: Path,
                    changes: list[dict] | None = None) -> str:
        """The step's prompt, shrunk (earlier replies and diffs first) to fit the agent's prompt limit."""
        adapter = self.adapters[a["adapter"]]
        notes = adapter.prompt_notes(a["policy"])
        images = image_note(self._images_for(task, a), getattr(adapter, "attaches_images", False))
        if a.get("resume_session"):
            earlier = [step_summary(b, b["status"]) for b in task.data["assignments"][a["followup_of"] + 1:a["index"]]
                       if b["status"] not in ("pending", "skipped")]
        else:
            earlier = previous
        if not earlier:
            changes = None
        elif changes is None:
            changes = compute_changes(Path(ws["base"]), work, ws["manifest"])

        def build(replies_budget: int, diff_budget: int) -> str:
            so_far = describe_changes(changes, diff_budget) if changes is not None else None
            if a.get("resume_session"):
                return build_followup_prompt(a["message"], a["policy"]["read_only"], a.get("was_read_only", False),
                                             earlier, so_far, notes, replies_budget, images)
            return build_prompt(a["role"], task.data["objective"], earlier, a["policy"]["read_only"], so_far, notes,
                                replies_budget, images)

        replies_budget, diff_budget = EARLIER_REPLIES_BUDGET, MAX_PROMPT_DIFF
        prompt = build(replies_budget, diff_budget)
        limit = adapter.max_prompt_chars()
        full = len(prompt)
        while limit and len(prompt) > limit and (replies_budget or diff_budget):
            over = len(prompt) - limit
            diff_budget = max(0, diff_budget - over) if diff_budget else 0  # diffs go first: agents can read files
            if diff_budget == 0:
                replies_budget = max(0, replies_budget - over)
            prompt = build(replies_budget, diff_budget)
        if len(prompt) < full:
            self.emit(task, "agent.stderr", {"assignment": a["index"], "text": (
                f"{adapter.display_name} takes prompts of at most {limit} characters here, so earlier replies and "
                f"diffs were shortened ({full} -> {len(prompt)} characters).")})
        return prompt

    @staticmethod
    def _images_for(task: EngineTask, a: dict) -> list[dict]:
        """The images a step is shown: a follow-up its own message's, every step of the first lineup
        the objective's (a resumed conversation has already seen the objective's)."""
        return (a.get("attachments") if a.get("resume_session") else task.data.get("attachments")) or []

    def _run_assignment(self, task: EngineTask, a: dict, work: Path, prompt: str) -> str:
        adapter = self.adapters[a["adapter"]]
        a["status"] = "running"
        a["started_at"] = time.time()
        self.emit(task, "assignment.started", {"index": a["index"], "role": a["role"], "adapter": a["adapter"],
                                               "profile": a["profile"], "followup_of": a.get("followup_of")})
        slot = self.adapter_slots[a["adapter"]]
        while not slot.acquire(timeout=1):
            if task.cancel_event.is_set():
                a["status"] = "cancelled"
                return "cancelled"
        scratch = None
        try:
            checks = task.data.get("checks") or {}
            policy = dict(a["policy"])
            policy["env"] = checks.get("env") or {}
            policy["python_env_bin"] = checks.get("python_env_bin")
            if checks.get("python_env") and not checks.get("python_env_bin"):
                self.emit(task, "agent.stderr", {"assignment": a["index"], "text": (
                    "The project's Python environment has no usable Scripts/bin folder here (made on another OS?), "
                    "so commands use the engine's own Python and may miss the project's packages.")})
            if a.get("resume_session"):
                policy["resume_session"] = a["resume_session"]
            # absolute paths of the step's images in the handoff folder (for CLIs that attach them natively)
            policy["images"] = [str(p) for p in (work / HANDOFF_DIR / ATTACHMENTS_DIR / im["file"]
                                                 for im in self._images_for(task, a)) if p.is_file()]
            run_dir = work
            if getattr(adapter, "runs_checks", False):
                # checks run in a throwaway copy: what they write never enters the change set
                policy["commands"] = checks.get("verify_commands") or []
                scratch = work.parent / "checks"
                remove_workspace(scratch)
                shutil.copytree(work, scratch, symlinks=True)
                run_dir = scratch
            try:
                run_id = adapter.start_task(task.public(), prompt, run_dir, policy,
                                            log_path=task.dir / f"assignment-{a['index']}.log")
            except AdapterUnavailable as exc:
                a["status"], a["error"], a["finished_at"] = "failed", str(exc), time.time()
                self.emit(task, "assignment.finished", {"index": a["index"], "status": "failed", "error": str(exc)})
                return "failed"
            task.current = (adapter, run_id)
            if task.cancel_event.is_set():
                adapter.cancel(run_id)
            for ev in adapter.stream_events(run_id):
                payload = dict(ev.get("payload") or {})
                payload["assignment"] = a["index"]
                if not a.get("session_id"):  # saved at once, so a cancelled run can still be continued
                    sid = next((payload[k] for k in SESSION_KEYS if isinstance(payload.get(k), str) and payload[k]),
                               None)
                    if sid:
                        a["session_id"] = sid[:200]
                        self._save(task)
                self.emit(task, ev["type"], payload)
            result = adapter.get_result(run_id)
            task.current = None
            a["status"] = "cancelled" if task.cancel_event.is_set() else result["status"]
            a["result"] = self.redactor.redact(result.get("result") or "")[:MAX_RESULT]
            a["error"] = result.get("error")
            a["exit_code"] = result.get("exit_code")
            a["stats"] = result.get("stats") or {}
            a["finished_at"] = time.time()
            self.emit(task, "assignment.finished", {"index": a["index"], "status": a["status"],
                                                    "exit_code": a.get("exit_code"), "error": a["error"],
                                                    "reply_chars": len(a["result"])})
            return a["status"]
        finally:
            slot.release()
            if scratch is not None:
                remove_workspace(scratch)
