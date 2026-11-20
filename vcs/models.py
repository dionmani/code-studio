from django.conf import settings
from django.db import models


class GitCredential(models.Model):
    """A user's HTTPS login for one Git host (e.g. github.com).

    The token is sealed with ``vcs.secrets`` (key stored outside the database)
    and is never returned to the browser.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="git_credentials")
    host = models.CharField(max_length=255, help_text="Host name, optionally with :port (e.g. github.com)")
    username = models.CharField(max_length=128)
    token_sealed = models.TextField()
    token_hint = models.CharField(max_length=8, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["host"]
        constraints = [models.UniqueConstraint(fields=["user", "host"], name="uniq_git_credential_per_host")]

    def __str__(self):
        return f"{self.username}@{self.host}"


class CommitMessagePreference(models.Model):
    """Which agent (and model) writes a user's AI commit messages. Blank = automatic/default."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="commit_message_preference")
    adapter = models.CharField(max_length=64, blank=True, help_text="Agent identifier; blank = first available")
    model = models.CharField(max_length=100, blank=True, help_text="Model name/alias; blank = the agent's default")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {self.adapter or 'auto'}/{self.model or 'default'}"
