# AGENTS.md — Instructions for coding agents

## Mission
Implement Code Studio according to `PROJECT_DESIGN.md` and `IMPLEMENTATION_PLAN.md`. This is a filesystem-first, Django-based browser IDE with a separately deployable, interchangeable CLI agent orchestration service.

## Read first
1. `PROJECT_DESIGN.md` — requirements and architecture.
2. `IMPLEMENTATION_PLAN.md` — phase order and acceptance tests.
3. `CONTEXT.md` — decisions and outstanding unknowns.
4. `README.md` — environment and setup.

## Non-negotiable rules
- Do not replace Django with a Node.js application. Browser-side JavaScript is acceptable when needed; keep local development simple.
- Do not tightly couple the IDE to Orkestra internals. Use a versioned adapter/bridge.
- Do not require Git or GitHub to open, edit or save a project.
- Do not silently initialize Git, push changes, merge branches or deploy images.
- Never operate outside an approved filesystem root; protect against `..` traversal and symlink escapes.
- Never give an LLM or subprocess unrestricted host filesystem, Docker socket or credential access.
- Do not overwrite external file edits without conflict detection.
- Do not claim Antigravity CLI support until verified with an actual supported interface.
- Do not invent upstream engine APIs or capabilities; inspect the source and document findings.
- No agent-generated edits to the primary project without a reviewable change set and explicit approval.

## Coding practices
- Python: clear type hints, small services, Django conventions, explicit error handling.
- Keep views thin; put filesystem, Git and engine interactions behind services/interfaces.
- Use environment variables for secrets; provide `.env.example` with placeholders only.
- Add migrations for schema changes and tests for every new endpoint/service.
- Prefer incremental, reviewable commits and minimal dependencies.
- Document platform-specific behavior for Windows development and Unraid deployment.

## Tests required
- Filesystem containment including traversal, symlinks and disallowed roots.
- Authorization on project/file/task/terminal endpoints and WebSockets.
- Save conflicts and atomic-write/recovery behavior.
- Git and unmanaged-folder workflows.
- Engine offline/timeout/restart/cancel behavior.
- Task events ordering and redaction.
- Change-set approval and rollback behavior.

## Workflow for each task
1. State assumptions and inspect existing code.
2. Propose the smallest safe implementation and tests.
3. Implement without unrelated refactors.
4. Run relevant tests; report exact commands and results.
5. Summarize changed files, risks, and follow-up tasks.
6. Ask before making architectural deviations or destructive changes.

## First assignment for Codex
Perform **Phase 0 technical validation** from `IMPLEMENTATION_PLAN.md`. Inspect Orkestra's actual repository and verify license, Python packaging, supported agents, executable invocation, worktree requirements and feasibility of a separate service bridge. Do not build an adapter against guessed APIs. Produce `docs/ENGINE_EVALUATION.md` with evidence and a go/no-go recommendation, then begin the Phase 1 Django scaffold only after the integration assumptions are documented.
