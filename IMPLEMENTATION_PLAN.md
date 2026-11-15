# Code Studio — Implementation Plan

## Delivery approach
Build thin, independently testable vertical slices. Do not implement the entire UI before validating file operations and the agent bridge. Each milestone requires a runnable demonstration and passing tests.

## Phase 0 — Technical discovery and risk reduction
**Goal:** Validate upstream assumptions before committing to engine internals.

Tasks:
1. Inspect Orkestra upstream repository and its exact license, version, maintenance and dependency tree.
2. Identify public APIs/library entry points, subprocess invocation, state persistence and worktree assumptions.
3. Verify Claude Code and Codex invocation in supported noninteractive modes; investigate whether Antigravity has a supported CLI/headless adapter.
4. Prototype a minimal Python wrapper that can launch one harmless task in an isolated test folder and capture structured progress, cancellation and errors.
5. Determine how unmanaged folders can use staged copies/snapshots independently of Orkestra.
6. Produce `docs/ENGINE_EVALUATION.md`, including source links, reproducible commands, licensing assessment, compatibility matrix and go/no-go decision.

**Exit gate:** Evidence-based decision on Orkestra; no guessed APIs; one real engine task or documented blocker; fallback design identified.

## Phase 1 — Django project foundation
Tasks:
1. Create Django project, `projects`, `workspace`, `agents`, `audit` apps (names may be refined).
2. Add authentication, settings via environment, logging, health check and `.env.example`.
3. Implement approved filesystem roots and project registration using relative paths.
4. Add path containment service and tests for traversal/symlink escapes.
5. Add Docker Compose for development/Unraid, with explicit read/write mounts and no Docker socket.
6. Document Windows local run steps and Unraid deployment mapping.

**Exit gate:** `python manage.py runserver` works locally; users can register and list allowlisted projects; path-security tests pass.

## Phase 2 — Useful standalone IDE
Tasks:
1. Project tree and file API: list/read/create/rename/delete with access checks.
2. Browser editor with tabs, syntax highlighting, save and unsaved-change warnings.
3. Optimistic concurrency tokens to prevent external-edit overwrites.
4. Project search with bounded results and exclusions.
5. Git status/diff/commit/pull/push for Git repositories; hide irrelevant controls for unmanaged folders.
6. Terminal gateway scoped to project with auth, output bounds, timeouts and explicit command policy.

**Exit gate:** Open a real mounted test project, edit/save and reopen files; test unmanaged and Git modes; confirm GitHub Actions untouched.

## Phase 3 — Agent service integration
Tasks:
1. Implement versioned engine client interface and fake adapter for deterministic tests.
2. Implement service bridge to approved engine, based on Phase 0 findings.
3. Add adapter discovery and role/profile configuration; capability-gate unsupported providers.
4. Implement task start, status, cancel, logs/events and reconnect behavior.
5. Stage edits in isolated workspaces; generate diffs and require approval to apply.
6. Add audit events, secret redaction and failure recovery.

**Exit gate:** Real CLI agent executes a safe task on a staging copy; IDE shows progress; primary project remains unchanged until approval.

## Phase 4 — Collaboration and isolation
Tasks:
1. Task decomposition and role assignment (architect/developer/reviewer or custom).
2. Structured agent messages and review findings.
3. Sequential review loop; then bounded parallel execution.
4. Git worktrees for Git projects; staged filesystem copies and snapshots for unmanaged projects.
5. Conflict detection, approval workflow, rollback and task history.

**Exit gate:** Two different agents complete implementation/review on a test repository; changes are reviewed, applied and reversible. Repeat on an unmanaged folder.

## Phase 5 — Operational hardening
Tasks:
1. Docker Compose production config, health checks, backups and persistent volumes.
2. UID/GID permissions and read/write validation on Unraid mounts.
3. Authentication, TLS deployment guidance, audit retention and least privilege.
4. Resource limits, cancellation escalation, process cleanup, log retention and monitoring.
5. End-to-end regression tests, documentation and recovery exercises.

**Exit gate:** Reproducible Unraid deployment; IDE works while engine is down; no accidental project-root escape; recoverable task failures.

## Initial issue backlog (suggested order)
- CS-001 Audit Orkestra and write engine evaluation.
- CS-002 Create Django skeleton and local run documentation.
- CS-003 Implement root allowlist and path validator.
- CS-004 Register/list/open local projects.
- CS-005 File API with conflict detection.
- CS-006 Browser editor with save and tabs.
- CS-007 Git/unmanaged mode detection and Git status.
- CS-008 Safe terminal proof of concept.
- CS-009 Engine adapter protocol and fake test engine.
- CS-010 Real engine bridge and task event streaming.
- CS-011 Staged change-set review/approval.
- CS-012 Agent role configuration and collaboration workflow.

## Quality gates for every milestone
- Document exact run/test commands and outcomes.
- Add unit/integration tests for behavior and security boundaries.
- Demonstrate functionality with a real project directory (safe fixture).
- Confirm no unexpected writes outside the approved project root.
- Update `CONTEXT.md` and README when decisions or setup change.
