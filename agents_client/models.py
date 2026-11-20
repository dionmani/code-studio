import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver


class AgentAdapter(models.Model):
    """An engine-side CLI adapter, mirrored from the engine's /agents probe."""

    identifier = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128, blank=True)
    executable = models.CharField(max_length=1024, blank=True, help_text="Reported by engine probe")
    config = models.JSONField(default=dict, blank=True)
    capability_snapshot = models.JSONField(default=dict, blank=True)
    available = models.BooleanField(default=False)
    version = models.CharField(max_length=128, blank=True)
    enabled = models.BooleanField(default=True)
    last_probed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["identifier"]

    def __str__(self):
        return self.identifier


class AgentProfile(models.Model):
    """User-defined role binding: which adapter plays which role, and how."""

    name = models.CharField(max_length=128, unique=True)
    adapter = models.ForeignKey(AgentAdapter, on_delete=models.CASCADE, related_name="profiles")
    role = models.CharField(max_length=64, default="implementer")
    execution_policy = models.JSONField(default=dict, blank=True,
                                        help_text='{"model": "...", "read_only": false, "timeout_seconds": 900}')
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.adapter_id}/{self.role})"


class Task(models.Model):
    STATES = [(s, s) for s in ("submitting", "queued", "preparing", "running", "awaiting_review", "completed",
                               "failed", "cancelled", "applied", "rejected", "interrupted", "engine_offline")]
    TERMINAL = {"awaiting_review", "completed", "failed", "cancelled", "applied", "rejected", "interrupted",
                "engine_offline"}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="tasks")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    objective = models.TextField(max_length=20000)
    state = models.CharField(max_length=32, choices=STATES, default="submitting")
    engine_task_id = models.CharField(max_length=64, blank=True)
    workspace_ref = models.CharField(max_length=255, blank=True)
    base_revision = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True, max_length=4000)
    last_event_seq = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    changes_summary = models.JSONField(default=list, blank=True, help_text="Files the agents changed (path/action)")
    # the engine still holds this task's staging copy, so a step's conversation can be continued
    can_follow_up = models.BooleanField(default=False)
    # images sent with the objective: [{"name", "file", "media_type", "size"}] (files in attachments_dir)
    attachments = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def attachments_dir(self) -> Path:
        return Path(settings.DATA_DIR) / "task_attachments" / str(self.id)


@receiver(post_delete, sender=Task)
def _remove_task_attachments(sender, instance: Task, **kwargs):
    shutil.rmtree(instance.attachments_dir, ignore_errors=True)


class TaskAssignment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="assignments")
    profile = models.ForeignKey(AgentProfile, null=True, on_delete=models.SET_NULL)
    adapter_identifier = models.CharField(max_length=64)
    role = models.CharField(max_length=64)
    order = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, default="pending")
    # the agent's final reply (redacted) and run facts, for the post-task summary
    result = models.TextField(blank=True)
    error = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    read_only = models.BooleanField(default=False)
    # conversation id the agent's CLI reported (needed to continue it in a follow-up)
    session_id = models.CharField(max_length=200, blank=True)
    # follow-up steps: which step's conversation they continue, and what the user said
    followup_of = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField(blank=True, max_length=20000)
    # images sent with the follow-up message (same records as Task.attachments)
    attachments = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["order"]


class TaskEvent(models.Model):
    """Bounded, redacted copy of engine events (full logs stay with the engine)."""

    MAX_PAYLOAD_CHARS = 8000

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    sequence = models.IntegerField()
    type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    timestamp = models.DateTimeField()

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["task", "sequence"], name="uniq_task_event_seq")]


class ChangeSet(models.Model):
    STATUSES = [(s, s) for s in ("pending", "applied", "partially_applied", "rejected", "conflict", "failed")]

    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name="changeset")
    base_revision = models.CharField(max_length=64, blank=True)
    artifact_path = models.CharField(max_length=1024, help_text="JSON artifact in the data dir")
    summary = models.JSONField(default=list)
    approval_status = models.CharField(max_length=32, choices=STATUSES, default="pending")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(null=True, blank=True)
    snapshot_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class DefaultTeam(models.Model):
    """A user's saved agent lineup for new tasks (Settings > Default team).

    ``steps`` is an ordered list of ``{"profile_id": int, "role": str}``. The task
    form starts from it; picks changed there apply to that one task only.
    """

    MAX_STEPS = 10

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="default_team")
    steps = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"default team of {self.user}"
