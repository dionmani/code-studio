# Code Studio — Project Design Document

**Status:** Initial specification / architecture proposal
**Version:** 0.1
**Date:** 2026-11-15

## 1. Vision
Build a self-hosted, browser-based development environment that can replace VS Code for the owner's everyday workflow while coordinating multiple interchangeable CLI coding agents. Projects are **filesystem-first**: the application opens and edits real directories mounted from an Unraid share. Git and GitHub are optional. Existing GitHub Actions and container image builds remain unchanged.

## 2. Product principles
1. The IDE remains useful when the agent engine is offline.
2. The IDE and agent engine are separate processes with a documented, versioned integration contract.
3. Never require GitHub or even Git to open a project.
4. Agent roles are configurable; no provider is hard-coded to a role.
5. Human approval is required before destructive operations, merges into the working project, pushes, or deployment.
6. Favor a straightforward Windows Django development workflow (`python manage.py runserver`).
7. The browser is a client, not a direct filesystem or shell authority.

## 3. Scope
### MVP
- Login and project registry, with explicitly configured filesystem roots.
- File tree, open/edit/save, tabs, syntax highlighting, search, unsaved-change warnings.
- Safe file operations (create, rename, delete with confirmation).
- Git status, diff, commit and pull/push for Git projects; no Git prompts for unmanaged folders.
- Integrated terminal with restricted execution scope.
- Agent engine adapter with health check, agent discovery, start/cancel task, event stream and task history.
- Agent role assignments for Claude Code, Codex and Antigravity **where supported by verified CLI adapters**.
- Reviewable proposed changes and approval before applying to the primary workspace.

### Later
- Concurrent task graphs, agent-to-agent messaging, multiple worktrees, collaborative review loops.
- Resource/cost telemetry, mobile monitoring, richer debugging, extensions, remote collaboration.

### Non-goals for MVP
- Full VS Code extension compatibility, language server parity, autonomous production deployment, unrestricted shell access, automatic GitHub repository creation.

## 4. Architecture
```text
Browser
  | HTTPS / WebSocket
Django IDE service
  |-- Project registry, auth, file API, Git API, terminal gateway
  |-- Browser editor (Monaco or CodeMirror; decide after prototype)
  |-- Agent integration client (versioned contract)
  |
  +---- Agent engine service (candidate: Orkestra; verify before adoption)
           |-- provider/CLI adapters
           |-- task orchestration, reviews, execution events
           +-- isolated workers/workspaces

Allowed filesystem root (Unraid host): /mnt/user/Projects
Container path: /workspaces
Git remotes: GitHub, optional
```
The IDE owns projects and user-facing state. The engine owns task execution and agent coordination. Avoid direct imports from an engine's internal Python modules into Django; communicate through a stable interface (HTTP + event stream or a small local RPC bridge).

## 5. Technology choices
- Backend: Django / Python; Django ASGI for WebSockets where needed.
- UI: Django templates + Bootstrap 5 initially; browser editor selection based on licensing, integration and build complexity.
- Database: PostgreSQL in production; SQLite permitted for early local development.
- Real-time: Django Channels or another ASGI WebSocket implementation; choose after proof of concept.
- Agent engine: Orkestra is **candidate**, not an approved dependency until audited.
- Jobs: separate process/queue only if needed; avoid introducing Celery/Redis before requirements justify them.
- Deployment: Docker Compose on Unraid; bind-mount only approved project roots.
- Source control: staged Dulwich library integration on native Windows, approved
  2026-11-16 in place of the initial local-Git-CLI proposal. Repository-provided
  hooks/helpers/configuration are not executed; approved changes pass through
  native filesystem conflict checks and durable review journals. GitHub remains
  optional and Actions continue unchanged. See `docs/GIT_EVALUATION.md`.

## 6. Filesystem contract
- Admin configures one or more **allowlisted** root directories. Project paths are always descendants of an allowlisted root.
- Resolve and validate canonical paths on every operation; reject traversal and symlink escapes.
- Do not expose the host root, Docker socket, SSH keys or secrets by default.
- Support files that are not in Git repositories.
- Detect concurrent external edits before save (mtime/hash or version token); require resolution rather than silently overwriting.
- Apply atomic writes where practical; maintain recovery snapshots for destructive changes.
- Keep editor changes and agent work isolated until approved.

## 7. Project modes
**Unmanaged folder:** direct editing with snapshots, agent staging copy, file-level diff and explicit apply. Do not silently initialize Git.

**Git repository:** editor works in the checked-out project; agents use isolated Git worktrees when supported. Review changes before merging/applying. GitHub is a remote, not the canonical live editor filesystem.

## 8. Agent roles and interoperability
Roles are user-defined labels and policies (e.g., architect, implementer, reviewer, tester), not fixed model identities. An agent configuration identifies provider/CLI adapter, executable, model/options, permissions, working directory policy, timeout and concurrency limit.

Required adapter operations:
- `probe()` -> availability, version, capabilities, auth state when detectable
- `start_task(task, context, workspace, policy)` -> task ID
- `stream_events(task_id)` -> ordered events with timestamps
- `cancel(task_id)` -> cancellation result
- `get_result(task_id)` -> status, artifacts, changed files, errors

Capabilities may differ by CLI. **Antigravity's usable CLI/headless integration must be validated**; do not assume a stable public CLI or automation interface. Show unavailable capabilities clearly rather than simulating support.

## 9. Engine integration API (proposed, subject to spike)
- `GET /health` — health and API version
- `GET /agents` — installed adapters and capabilities
- `POST /tasks` — submit a task with project ID, role assignments and workspace policy
- `GET /tasks/{id}` — state and result
- `GET /tasks/{id}/events` — event stream or WebSocket
- `POST /tasks/{id}/cancel` — cancel execution
- `GET /tasks/{id}/changes` — proposed diffs
- `POST /tasks/{id}/approve` — approved apply action, validated by IDE

Authentication between services is mandatory; the engine must never trust client-supplied absolute paths. The IDE maps project IDs to approved server-side paths.

## 10. UI layout
- Left sidebar: projects, files, search, Git.
- Center: multi-tab editor, diff viewer.
- Bottom panel: terminal, problems, task logs.
- Right panel: agent team, task queue, plans, review comments, approvals.
- Header: project, branch, connection/engine status, user profile.
- Responsive mobile view focuses on monitoring and approvals, not full code editing parity.

## 11. Security
- Require authentication, session protection, CSRF defense, WebSocket authorization and audit logs.
- Default agents to least-privilege execution, per-project working directories and no host Docker socket.
- Distinguish read-only analysis, staged edits and privileged commands.
- Require explicit approval for delete, merge, push, shell escalation and access outside project scope.
- Never stream environment secrets, auth tokens or credential files to the UI or model prompts.
- Treat repository files, agent output and model instructions as untrusted input.
- Add task timeouts, process-group termination and bounded output logs.
- Do not expose the application publicly without TLS and access controls.

## 12. Data model (initial)
- `Project`: name, root alias, relative path, git mode, created_at.
- `AgentAdapter`: identifier, executable/config reference, capability snapshot, enabled.
- `AgentProfile`: name, adapter, role, execution policy, enabled.
- `Task`: project, requester, objective, state, timestamps, workspace reference.
- `TaskAssignment`: task, agent profile, role, status.
- `TaskEvent`: task, sequence, type, payload (redacted), timestamp.
- `ChangeSet`: task, base revision/hash, diff/artifact reference, approval status.
- `AuditEvent`: actor, project, action, outcome, timestamp.
Store large logs/diffs as bounded files or artifacts, not unbounded database rows.

## 13. Definition of done for MVP
A user can start Django locally, register an approved project folder, open/edit/save files, observe external-change conflicts, run a scoped terminal, view Git status for Git projects, use unmanaged folders without Git, and submit a task through a **real** engine adapter with visible output and explicit approval before changes land. Tests cover path containment, authorization, save conflicts, cancellation and rollback.

## 14. Open decisions and validation gates
1. Audit Orkestra repository: license, maintenance, API/library boundary, provider adapters, Git assumptions and subprocess model.
2. Verify the actual installed Claude Code, Codex and Antigravity automation interfaces and licensing/terms.
3. Choose Monaco vs CodeMirror after testing Windows development without a mandatory Node.js runtime.
4. Choose engine bridge (HTTP wrapper vs other IPC) based on Orkestra's real interface.
5. Choose snapshot mechanism and recovery retention policy.
6. Decide terminal implementation and security constraints.
7. Validate Unraid bind-mount permissions and UID/GID behavior.

**Decision rule:** If Orkestra is incompatible or unsuitable, retain the engine API contract and evaluate another engine rather than coupling Django to a specific codebase.
