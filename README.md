# Code Studio

A self-hosted, browser-based IDE that edits real folders and coordinates CLI
coding agents (Claude Code, Codex, and Antigravity when available). Every
agent change waits for human review before it touches your project. See
`PROJECT_DESIGN.md` for the specification and `docs/DECISIONS.md` for how each
open design gate was resolved.

```
Browser ──HTTP/WebSocket──> Django IDE (daphne ASGI)         Agent Engine (FastAPI)
                             projects, files, Git (Dulwich),  ──signed HTTP+SSE──>  adapters: claude-code,
                             terminal, review/apply, audit                           codex, antigravity
                                    │                                                staging workspaces
                                    └── data dir: DB, snapshots, change sets, engine token
```

## Docker / Unraid (always-on)

`.\scripts\deploy_unraid.ps1` builds and starts both services on the Unraid
server with your project share mounted. See [`docs/DOCKER.md`](docs/DOCKER.md).

## Run Locally (install from the server onto a Windows PC)

The server copy (Docker, or any copy with `CODE_STUDIO_DEBUG=0`) shows **Run Locally** in
the top bar. That page downloads a Windows package built from the server's own
source. Extract it and double-click `Install Code Studio.cmd`. The installer sets up
Python 3.12 (via `winget`) if needed and installs into `%LOCALAPPDATA%\Programs\CodeStudio`:

| Folder | Contents | On update |
|---|---|---|
| `app\` | program files + `BUILD_INFO.json` | replaced |
| `venv\` | Python environment | kept, packages synced |
| `data\` | database, secrets, snapshots | kept (database backed up to `data\backups\` first) |
| `config.json` | ports, update server, download token, preferences | kept; only missing keys added |

**Versions.** Each copy reports `VERSION` plus a *build* fingerprint of its source files
(`localinstall/version.py`), so a changed server is detected without bumping a version
number. `GET /api/version` is public and returns only `{version, build}`.

**Updates.** A local copy checks its server when it starts (and shows *Update available*
in the top bar). Updating downloads the current package using the copy's download token
(`Authorization: Bearer`, signed, tied to the user who downloaded it). It then runs that
package's own installer in update mode. If the update fails, the previous `app\` and
database are restored. The Start Menu also has **Update Code Studio** and
**Uninstall Code Studio** (uninstall keeps `data\` unless you choose otherwise).

Scripts: `localinstall/windows/` (`install.ps1`, `launch.ps1`, `update.ps1`,
`uninstall.ps1`, `common.ps1`). They are Windows PowerShell 5.1 compatible and ASCII only.

## Quick start (Windows, PowerShell)

```powershell
cd C:\Users\Dion\Documents\cursorprojects\code-studio
.\scripts\setup.ps1 -AdminPassword '<strong password>' -Root 'C:\Users\Dion\Documents\cursorprojects'
# terminal 1 — agent engine (optional; the IDE works without it)
.\scripts\run_engine.ps1            # = .venv\Scripts\python -m agent_engine --port 8765
#   Antigravity: log in once with 'agy', then "Turn on" in Settings > Agent team (no restart)
# terminal 2 — IDE
.\scripts\run_ide.ps1               # = .venv\Scripts\python manage.py runserver 127.0.0.1:8888
```

Then open http://127.0.0.1:8888, sign in, and register a folder from
**Projects**. Folders with a `.git` directory get Git features. Other folders
stay unmanaged; Code Studio never initialises Git.

`setup.ps1` runs these steps (you can also run them by hand):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py bootstrap --username admin --password <pw> --root projects=D:\Projects [--project projects:my-app]
```

Both services share `%LOCALAPPDATA%\CodeStudio`, which holds the SQLite DB,
snapshots, change sets, engine workspaces and the auto-generated service
token.

| Variable | Purpose |
|---|---|
| `CODE_STUDIO_DATA_DIR` | data dir (must be outside every project root) |
| `CODE_STUDIO_ENGINE_URL` | IDE → engine URL (default `http://127.0.0.1:8765`) |
| `CODE_STUDIO_ENGINE_TOKEN` | shared service token (default: `<data dir>\engine_token`) |
| `CODE_STUDIO_ENGINE_ALLOWED_ROOTS` | optional `;`-list the engine will stage from |
| `CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY` | legacy: only seeds the Antigravity switch once (now toggled in Settings > Agent team) |
| `CODE_STUDIO_DEBUG`, `CODE_STUDIO_ALLOWED_HOSTS`, `CODE_STUDIO_SECRET_KEY` | Django settings |

## Testing

```powershell
.\.venv\Scripts\python -m pytest                          # 114 unit/integration tests
.\.venv\Scripts\python -m playwright install chromium     # once
.\.venv\Scripts\python e2e\run_e2e.py                     # live servers + real browser (fixture agent)
.\.venv\Scripts\python e2e\run_e2e.py --real-agents       # ...plus a real Claude Code task (small cost)
.\.venv\Scripts\python e2e\real_adapters_smoke.py codex claude-code   # real CLI adapters via the engine
```

## Layout
| Path | Contents |
|---|---|
| `codestudio/` | settings, URLs, ASGI (HTTP + WebSocket routing) |
| `projects/` | allowlisted roots, projects, **canonical path containment** (`paths.py`) |
| `files/` | tree/read/save (version tokens, 409 conflicts, atomic writes), create/rename/delete, search, snapshots |
| `vcs/` | Dulwich Git with hooks/filters disabled |
| `terminal/` | scoped WebSocket terminal + command policy; local-only System terminal (ConPTY/pty + xterm.js) |
| `agents_client/` | engine client, profiles, tasks, SSE proxy, validated apply/rollback |
| `audit/` | `AuditEvent` + redacted recorder |
| `agent_engine/` | standalone engine service (no Django) |
| `cs_common/` | signing, redaction, process-tree helpers shared by both services |
| `static/ide`, `templates/` | Bootstrap 5 + vanilla JS UI; Monaco and xterm.js vendored under `static/vendor` |

## Security notes
* Auth is required everywhere. CSRF is enforced on API writes, WebSockets are auth- and origin-checked, and a strict CSP is in place (no inline scripts).
* Every file, terminal, Git and agent operation resolves canonical paths and rejects traversal, symlink/junction escapes, ADS and device names.
* Secrets are redacted from logs, terminal output and agent events. Credential files (`.env`, keys, …) are never copied to agents and never applied from them.
* Run locally (`127.0.0.1`), or behind TLS and access control. The terminal is a policy guard, not an OS sandbox; for untrusted users, deploy in a container with only the approved roots mounted.
* The **System terminal** tab is a real, unrestricted shell with your user account's permissions. It is only offered to loopback clients and is on by default only for a local copy (`CODE_STUDIO_SYSTEM_TERMINAL=1` turns it on elsewhere, e.g. for `manage.py runserver`).
