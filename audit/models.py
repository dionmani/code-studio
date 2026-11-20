from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    OUTCOMES = [(o, o) for o in ("success", "denied", "error", "conflict", "started", "cancelled")]

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64, db_index=True)
    target = models.CharField(max_length=1024, blank=True)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default="success")
    detail = models.JSONField(default=dict, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.actor} {self.action} {self.outcome}"
