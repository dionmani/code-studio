# Code Studio

A planned self-hosted, filesystem-first browser IDE with interchangeable CLI coding agents.

> **Project status:** Specification/starter documents only. The application has not yet been implemented. Commands below describe the intended workflow after the Django scaffold is created.

## Goals
- Work directly on Unraid-mounted project folders, with or without Git.
- Preserve GitHub repositories and existing GitHub Actions image builds.
- Provide a browser editor, terminal, Git controls and task dashboard.
- Keep Django IDE separate from the CLI agent orchestration engine.
- Support configurable agent roles for Claude Code, Codex and Antigravity where verified interfaces exist.

## Repository documents
- [`PROJECT_DESIGN.md`](PROJECT_DESIGN.md) — product/technical specification.
- [`AGENTS.md`](AGENTS.md) — rules and starting instructions for coding agents.
- [`CONTEXT.md`](CONTEXT.md) — decisions and project status.
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — phases and exit criteria.

## Intended local development (Windows)
Once the scaffold exists:
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```
These commands **will not run yet** because the application files and requirements have not been created.

## Intended Unraid deployment
The application will use Docker Compose with a restricted project bind mount. Example intended mapping:
```yaml
volumes:
  - /mnt/user/Projects:/workspaces:rw
```
This is illustrative, **not a complete or production-ready Compose file**. Mount only approved directories; enforce application-level project allowlists, per-project authorization and safe subprocess isolation.

## Agent engine
**Candidate:** [Orkestra](https://github.com/andyyaro/orkestra). Its API, license, supported providers and integration feasibility must be verified in Phase 0. The Django IDE will depend on a stable adapter contract, not engine-specific internals. Unsupported CLI integrations must be reported honestly.

## First Codex prompt
```text
Read AGENTS.md, PROJECT_DESIGN.md, CONTEXT.md, IMPLEMENTATION_PLAN.md and README.md.
Begin with Phase 0 only: inspect the actual Orkestra upstream repository and its license, adapters, CLI invocation, worktree assumptions and service-integration options. Verify claims with source references. Write docs/ENGINE_EVALUATION.md with a go/no-go recommendation and a minimal proof-of-concept plan. Do not invent APIs, modify unrelated projects, or implement an unverified Antigravity adapter. Once Phase 0 is documented, propose the smallest Phase 1 Django scaffold and tests for approval.
```

## Development rule
The IDE must always work without the agent engine running. Git and GitHub must never be prerequisites for editing a project folder.
