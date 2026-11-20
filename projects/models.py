from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


class AllowlistedRoot(models.Model):
    """Admin-approved filesystem root. Every project lives beneath one."""

    alias = models.CharField(max_length=64, unique=True,
                             validators=[RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")])
    path = models.CharField(max_length=1024, help_text="Canonical absolute path (resolved on save)")
    description = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["alias"]

    def __str__(self):
        return f"{self.alias} -> {self.path}"


class Project(models.Model):
    GIT = "git"
    UNMANAGED = "unmanaged"
    GIT_MODES = [(GIT, "Git repository"), (UNMANAGED, "Unmanaged folder")]

    name = models.CharField(max_length=128)
    root = models.ForeignKey(AllowlistedRoot, on_delete=models.PROTECT, related_name="projects")
    relative_path = models.CharField(max_length=1024, blank=True,
                                     help_text="Path relative to the allowlisted root ('' = root itself)")
    git_mode = models.CharField(max_length=16, choices=GIT_MODES, default=UNMANAGED)
    LINE_ENDINGS = [("auto", "Auto-detect"), ("crlf", "Windows checkout (CRLF files, LF in Git)"),
                    ("lf", "As stored in Git (no conversion)")]
    git_line_endings = models.CharField(
        max_length=8, choices=LINE_ENDINGS, default="auto",
        help_text="How working files relate to what Git stores (Git's core.autocrlf). 'auto' uses the "
                  "repository's own setting, otherwise detects a Windows (CRLF) checkout.")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                              related_name="owned_projects")
    # Settings > Project checks: {"verify_commands": [...], "env": {...}, "exclude": [...], "python_env": ".venv"}
    agent_checks = models.JSONField(default=dict, blank=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="projects")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["root", "relative_path"], name="uniq_project_location")]

    def __str__(self):
        return self.name

    def user_can_access(self, user) -> bool:
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return self.owner_id == user.id or self.members.filter(pk=user.pk).exists()
