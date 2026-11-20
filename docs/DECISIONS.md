# Design gate resolutions (PROJECT_DESIGN.md §14)

Evaluated on the target machine: Windows 11 Home 10.0.26200, Python 3.12.10,
2026-11-17.

## Gate 1 & 4 — Agent engine (Orkestra) and bridge
* **Finding:** Orkestra is not installed locally (no package, no checkout among
  the local projects), so its licence, API boundary, Git assumptions and
  subprocess model could not be audited.
* **Decision:** Do not adopt it. Following the §14 decision rule, the contract
  was kept and a small standalone engine (`agent_engine/`, FastAPI + uvicorn)
  implements it. The bridge is **HTTP + Server-Sent Events** with HMAC
  service auth (see `docs/ENGINE_API.md`). The engine has no Django imports;
  shared code is limited to `cs_common` (signing, redaction, data-dir helpers).
* **Swap path:** any engine that implements `docs/ENGINE_API.md` can replace it;
  only `CODE_STUDIO_ENGINE_URL` changes.

## Gate 2 — CLI automation interfaces (probed, then run for real)
| CLI | Version | Headless interface used | Verified result |
|---|---|---|---|
| Claude Code | 2.1.326 | `claude -p --output-format stream-json --verbose --permission-mode acceptEdits` (`plan` for read-only), prompt on stdin. Auth via `claude auth status` (JSON). | Real task through the IDE UI and through the engine: file created in staging, reviewed, applied. |
| OpenAI Codex CLI | 0.169.0 | `node codex.js exec --json --skip-git-repo-check -C <ws> --sandbox workspace-write` (`read-only` for read-only), prompt on stdin. The npm `.cmd` shim is bypassed. Auth via `codex login status`. | Real task through the engine: file created in staging, reviewed. |
| Google Antigravity | `agy` 1.4.12 at first probe; reported 1.5.10 later the same day | `agy --print=<prompt> --output-format stream-json --mode plan\|accept-edits` exists. | **Not authenticated on this machine.** Every headless call started an interactive Google OAuth flow, waited 60 s and failed. Only the final `{"event":"result",...}` record was observed. There is no non-interactive auth check, so the adapter is **opt-in**: log in by running `agy` once, then start the engine with `scripts\run_engine.ps1 -EnableAntigravity` (sets `CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY=1`). **Re-verified after login (agy 1.5.10):** a real headless task created a file in staging and reached `awaiting_review`. Its probe never launches a prompt and reports `unavailable` with the reason. |

All adapters implement `probe()`, `start_task()`, `stream_events()`,
`cancel()` and `get_result()` (`agent_engine/adapters/base.py`):
* subprocess in a new process group;
* whole-tree kill on cancel and timeout (psutil);
* per-assignment timeout (5–3600 s);
* sanitised environment (secret-named variables dropped; only the adapter's own auth variables are kept);
* stdout/stderr redaction;
* bounded per-assignment log files.

A deterministic **test-fixture adapter** exists only when
`CODE_STUDIO_ENGINE_TEST_ADAPTER=1`. It is labelled "not an AI agent" and is
used by the automated tests.

Licensing and terms: the CLIs are used as installed by the owner, under the
owner's own accounts. Code Studio doesn't redistribute them.

**Codex sandbox caveat:** Codex's Windows sandbox runs under a restricted
identity. It cannot write into folders that carry an owner-only ACL, such as
folders made with `tempfile.mkdtemp()` (Python ≥ 3.12.4 on Windows). The
engine's own folders are created with a plain `mkdir`. Don't point
`CODE_STUDIO_DATA_DIR` at an owner-only-ACL folder.

## Gate 3 — Browser editor
**Monaco Editor 0.52.2**, vendored under `static/vendor/monaco/vs` from the
npm tarball (SHA-256 `c280cdcf…c000789e`) and loaded with Monaco's AMD
loader. No Node.js runtime or build step is needed. Monaco was chosen over
CodeMirror 6, whose ESM packages need a bundler. Monaco also provides the
side-by-side diff editor that the review and conflict flows use. Licence: MIT.
Bootstrap 5.3.3 is vendored the same way. It works under a strict
CSP (`script-src 'self'`, no `unsafe-eval`).

## Gate 5 — Snapshots and retention
File-backed snapshots live in `<data dir>\snapshots\<project id>\<UTC timestamp>-<id>\`
(`manifest.json` + `files/`), never inside a project. They are taken before:
delete, conflicting force-save, agent apply, and restore (so a restore can be
undone). Each entry records whether the path existed, so restoring also
removes files that an operation created. Retention: the newest 100 per
project, nothing older than 30 days, and the newest 10 always kept. Snapshots
over 50 MB are refused. Multi-file agent applies roll back automatically from
their snapshot if any write fails.

## Gate 6 — Terminal
**Line-oriented scoped terminal over an authenticated WebSocket**
(Channels/daphne). This was chosen over a full PTY so that every command can be
policy-checked and audited before it runs:
* each command runs as a child process (PowerShell or cmd) with its `cwd` inside the canonical project root;
* `cd` is a server built-in, confined to the root;
* path-like tokens (absolute, UNC, `~`, `..`, profile env-vars) must resolve inside the root;
* escalation, registry, service, link-creation, drive-mapping, Docker-mount and env-dump commands are refused;
* output is redacted and capped at 5 MB;
* commands time out after 10 min, Stop kills the process tree, and idle sessions close after 30 min;
* every command (allowed or denied) is written to `AuditEvent`.

**Limitation (stated honestly):** this is a policy guard, not an OS sandbox.
For untrusted users, run Code Studio in a container that only bind-mounts the
approved roots (as PROJECT_DESIGN.md §5 plans for Unraid). Interactive TUI
programs aren't supported in line mode.

### System terminal (local copy only, added 2026-11-20)
A second, **unrestricted** terminal for a local copy, where the IDE user is the
machine's own user and the scoped terminal mostly gets in the way (no persistent
`cd`/venv activation, no profile, rewritten paths, no interactive programs):
* one long-lived shell (PowerShell 7 if installed, else Windows PowerShell, or cmd)
  **with the user's profile**, in a real pseudo console, rendered with xterm.js;
* Windows uses the built-in ConPTY API via `ctypes` (`terminal/syspty.py`), so no
  third-party native DLL is needed (Smart App Control blocks unsigned ones, cf. `ujson`);
  POSIX uses the stdlib `pty` module;
* no command policy and no output redaction. The environment is the user's own
  minus `CODE_STUDIO_*` (engine token, secret key) and `DJANGO_SETTINGS_MODULE`,
  and Code Studio's own venv is removed from `PATH`;
* served only when `CODE_STUDIO_SYSTEM_TERMINAL` is on (default: on for a local copy,
  off otherwise) **and** the client address is loopback; the WebSocket is auth- and
  origin-checked like the scoped one. Session start/end are audited, keystrokes are not;
* closing the tab, switching project or Kill ends the whole process tree.

## Gate 7 — Unraid bind mounts and UID/GID
Not testable on this Windows workstation, so it's deferred to the Docker
Compose deployment. The code makes no POSIX-ownership assumptions: it uses
canonical-path containment only, and atomic writes preserve the existing file
mode.

## Dulwich (approved 2026-11-16)
See `docs/GIT_EVALUATION.md`.
