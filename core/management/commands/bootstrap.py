"""Local development bootstrap.

    python manage.py bootstrap --username admin --password <pw> \
        --root projects=C:\\Users\\me\\Projects [--project projects:my-app]

Creates/updates a superuser, allowlisted roots, optional project registrations
and default agent adapters/profiles. Idempotent.
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from agents_client.models import AgentAdapter, AgentProfile
from projects.models import AllowlistedRoot, Project
from projects.paths import PathSecurityError, canonical, normalize_relative, validate_root_path
from projects.service import detect_git_mode

DEFAULT_ADAPTERS = [("claude-code", "Claude Code"), ("codex", "OpenAI Codex CLI"), ("antigravity", "Google Antigravity")]
DEFAULT_PROFILES = [
    ("Claude implementer", "claude-code", "implementer", {"read_only": False, "timeout_seconds": 900}),
    ("Codex reviewer", "codex", "reviewer", {"read_only": True, "timeout_seconds": 900}),
    ("Codex implementer", "codex", "implementer", {"read_only": False, "timeout_seconds": 900}),
    ("Antigravity implementer", "antigravity", "implementer", {"read_only": False, "timeout_seconds": 900}),
]


class Command(BaseCommand):
    help = "Create a superuser, allowlisted roots, projects and default agent profiles"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", help="or set CODE_STUDIO_BOOTSTRAP_PASSWORD (keeps it off the command line)")
        parser.add_argument("--email", default="")
        parser.add_argument("--root", action="append", default=[], help="alias=absolute_path")
        parser.add_argument("--project", action="append", default=[], help="root_alias:relative/path[:name]")
        parser.add_argument("--test-adapter", action="store_true", help="also create a fixture-adapter profile")
        parser.add_argument("--first-run-only", action="store_true",
                            help="if any user exists, only ensure default adapters/profiles (container restarts)")

    def handle(self, *args, **opts):
        opts["password"] = opts["password"] or os.environ.get("CODE_STUDIO_BOOTSTRAP_PASSWORD") or None
        User = get_user_model()
        if opts["first_run_only"] and User.objects.exists():
            owner = User.objects.filter(is_superuser=True).order_by("pk").first() or User.objects.order_by("pk").first()
            self._defaults(owner, opts)
            self.stdout.write("existing installation: users/roots untouched; default profiles ensured")
            return
        user, created = User.objects.get_or_create(username=opts["username"], defaults={"email": opts["email"]})
        if created or opts["password"]:
            if not opts["password"]:
                raise CommandError("--password is required when creating the user")
            user.set_password(opts["password"])
        user.is_staff = user.is_superuser = True
        user.save()
        self.stdout.write(f"user: {user.username} ({'created' if created else 'updated'})")

        for spec in opts["root"]:
            alias, _, path = spec.partition("=")
            try:
                resolved = validate_root_path(path)
            except PathSecurityError as exc:
                raise CommandError(f"root {alias}: {exc}")
            root, _ = AllowlistedRoot.objects.update_or_create(alias=alias, defaults={"path": str(resolved),
                                                                                       "created_by": user})
            self.stdout.write(f"root: {root}")

        for spec in opts["project"]:
            parts = spec.split(":")
            alias, rel = parts[0], parts[1] if len(parts) > 1 else ""
            root = AllowlistedRoot.objects.get(alias=alias)
            rel = normalize_relative(rel)
            target = canonical(root.path) / rel if rel else canonical(root.path)
            name = parts[2] if len(parts) > 2 else (target.name or alias)
            project, _ = Project.objects.update_or_create(
                root=root, relative_path=rel,
                defaults={"name": name, "git_mode": detect_git_mode(target), "owner": user})
            project.members.add(user)
            self.stdout.write(f"project: {project.name} [{project.git_mode}]")

        self._defaults(user, opts)

    def _defaults(self, user, opts):
        adapters = list(DEFAULT_ADAPTERS)
        profiles = list(DEFAULT_PROFILES)
        if opts["test_adapter"]:
            adapters.append(("fixture", "Test fixture (deterministic, not an AI agent)"))
            profiles.append(("Fixture implementer", "fixture", "implementer", {"timeout_seconds": 120}))
        for ident, name in adapters:
            AgentAdapter.objects.get_or_create(identifier=ident, defaults={"display_name": name})
        for name, ident, role, policy in profiles:
            AgentProfile.objects.get_or_create(name=name, defaults={
                "adapter": AgentAdapter.objects.get(identifier=ident), "role": role, "execution_policy": policy,
                "created_by": user})
        self.stdout.write(self.style.SUCCESS("bootstrap complete"))
