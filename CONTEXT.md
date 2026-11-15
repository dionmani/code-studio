# Code Studio — Project Context

**Last updated:** 2026-11-15

## Why this exists
The owner currently develops with VS Code and uses GitHub for repositories and existing automated Docker image builds. The goal is to replace VS Code with a browser IDE that allows multiple CLI coding agents to work together while keeping the underlying files on an Unraid share.

## Confirmed decisions
- Browser-based, self-hosted IDE; Django/Python is preferred for familiar local testing via `python manage.py runserver`.
- IDE and agent orchestration engine are separate components.
- Agent identities are interchangeable with configurable roles. Desired providers: Claude Code, Codex and Antigravity.
- Local files are authoritative: mount an Unraid project share into the application.
- Windows-local project folders can be moved onto the share; no Windows companion agent needed initially.
- Projects may be Git-managed or unmanaged.
- GitHub remains optional source control; existing GitHub Actions and image-build pipeline are not to be replaced.
- Orkestra is the preferred **candidate** engine, pending technical verification.
- Rust is not a requirement; Python is preferred for this product's development workflow.

## Proposed but not yet confirmed
- Product working name: Code Studio.
- PostgreSQL for production; SQLite for early development.
- Bootstrap 5 plus Monaco or CodeMirror.
- Django Channels for live updates.
- Docker Compose deployment with mounted project roots.

## Known risks
- Orkestra may assume all projects use Git/worktrees.
- Antigravity may not expose a supported CLI/headless interface; verify rather than assume.
- Existing engine may not have a service API; may need a separate wrapper.
- Unmanaged projects need snapshot/staging-based review rather than Git worktrees.
- Browser terminals and filesystem access require strong authorization and process isolation.
- Multiple agents editing the same files can cause conflicts; use staged workspaces and approvals.

## Current project status
Planning complete enough to start Phase 0 investigation. No engine integration, Django application, deployment or tests have been implemented as part of this starter package.

## Next action
Open this repository in Codex. Ask it to read `AGENTS.md`, inspect the proposed Orkestra upstream, write an evidence-based `docs/ENGINE_EVALUATION.md`, and scaffold the Django project according to the implementation plan after the validation gate.

## Decision log
- 2026-11-15: Choose filesystem-first Unraid share rather than GitHub-only or Windows companion architecture.
- 2026-11-15: Keep GitHub Actions image builds unchanged.
- 2026-11-15: Separate IDE and orchestration engine; make agent roles configurable.
- 2026-11-15: Select Orkestra as candidate pending inspection, not as an irrevocable dependency.
