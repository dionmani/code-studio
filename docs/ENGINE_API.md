# Agent Engine API — contract v1

The IDE (Django) and the Agent Engine are separate processes. The IDE never
imports engine internals; it talks to this HTTP + SSE contract only, so the
engine can be swapped (e.g. for Orkestra once audited) without IDE changes.

Base URL: `CODE_STUDIO_ENGINE_URL` (default `http://127.0.0.1:8765`).

## Authentication (mandatory, every endpoint)

Requests are signed with a shared service token (`CODE_STUDIO_ENGINE_TOKEN`,
or the auto-generated `<data dir>\engine_token` file both services read).

| Header | Value |
|---|---|
| `X-CS-Timestamp` | Unix seconds; rejected if more than 300 s from engine time |
| `X-CS-Signature` | hex `HMAC-SHA256(token, "{ts}\n{METHOD}\n{raw path + ?query}\n{sha256(body)}")` |
| `X-CS-Api-Version` | `1` (other values → 400) |

Unsigned, mis-signed or stale requests get `401 {"error": "unauthorized"}`.
Browsers never talk to the engine; the IDE proxies everything.

## Path trust

The engine never trusts browser-supplied paths. The **IDE** resolves a
project ID to its canonical, allowlist-validated directory and sends that in
`project.root`. The engine re-validates it (absolute, exists, not a filesystem
root, does not overlap the engine data dir, and inside
`CODE_STUDIO_ENGINE_ALLOWED_ROOTS` when that is configured).

## Endpoints

### `GET /health`
`{"status":"ok","api_version":"1","engine_version":"0.1.0","engine":"code-studio-engine","tasks_running":0,"time":...}`

### `GET /agents`
Probe results per adapter:
`{"agents":[{"identifier","display_name","available","version","executable","auth_state","capabilities":{...},"notes":[...],"user_enabled"}]}`

`user_enabled` is `null` for adapters without an on/off switch, otherwise the switch's value.

### `PUT /agents/{identifier}`
Body `{"enabled": true|false}`. Turns a switchable adapter (today: `antigravity`) on or off at
runtime and returns `{"agent": <probe>}`. It applies to new tasks immediately, with no restart. Saved in
`<data>/engine/settings.json`. `404` for an unknown adapter, `400` for one without a switch or a bad body.

### `POST /tasks` → 201
```json
{
  "task_id": "optional hex id (IDE sends its own)",
  "objective": "text (≤ 20000 chars)",
  "requested_by": "username",
  "project": {"id": 1, "name": "app", "root": "C:\\canonical\\path", "git_mode": "git|unmanaged", "base_revision": "sha|null"},
  "assignments": [
    {"assignment_id": 0, "profile": "Claude implementer", "adapter": "claude-code", "role": "implementer",
     "policy": {"model": null, "read_only": false, "timeout_seconds": 900, "max_turns": null}}
  ],
  "workspace_policy": {"mode": "staging_copy"},
  "images": [{"name": "login-page.png", "data": "<base64>"}]
}
```
Assignments run sequentially in one isolated staging workspace; each later
assignment receives earlier results in its prompt. Returns the task record.

**Handoff folder.** Before every step the engine (re)writes `.codestudio-task/` in the staging copy
from its own record: `task.json` (objective, every step with role/agent/status/file, the step about to
run marked `current`, files changed so far), one `step-NN-<role>.md` per finished step with its complete
reply (and error, follow-up message), `changes.diff` (all diffs so far; sensitive files named only) and a
`README.md`. Prompts after the first step point agents at it. The folder is excluded from copies and
from the change set, and rewritten each step, so an agent editing it changes nothing. An editing step
that follows a successful review/critic/audit step is told to fix the findings and report each one as
fixed or not fixed.

**Images** (optional `images` on `POST /tasks` and on a follow-up): up to 6 PNG, JPEG, GIF or WebP images,
5 MB each and 15 MB together (`cs_common/images.py`; the IDE checks the same limits). The type is read
from the bytes, so SVG and anything else is refused with 400. The engine keeps them in its task folder
(`task-NN.<ext>` for the objective's, `stepNN-MM.<ext>` for a follow-up's) and copies them to
`.codestudio-task/attachments/` before every step, so they are never part of the change set. The task
record lists them as `attachments: [{name, file, media_type, size}]` (a follow-up's on its assignment).
Every step of the first lineup gets the objective's images; a follow-up gets its own message's. The prompt
lists the image paths and tells the agent to open them before starting. Claude Code and Antigravity read
them with their file tools; Codex also gets each one as `--image=<path>`.

How much of each earlier reply a later step sees *in its prompt* depends on the earlier step's role
(matched in the role name): architect/plan/design 20 000 characters, review/critic/audit 8 000, Verify
12 000, others 4 000. All earlier replies together are capped at 45 000; over that, one common cap is
found so short replies stay whole and long ones share the rest. A cut reply keeps its first two thirds
and last third around "[... N characters not shown here; the full reply is in
.codestudio-task/step-NN-<role>.md ...]". For CLIs with a prompt-size limit (Antigravity takes its prompt as one command-line
argument: 26 000 characters on Windows), diffs and then earlier replies are shortened to fit, and the
task log says so.

`project.checks` (optional, from Settings > Project checks; validated again by the engine):

```json
{"verify_commands": ["python manage.py makemigrations --check --dry-run", "python -m pytest -q"],
 "env": {"DJANGO_SETTINGS_MODULE": "app.settings_test"}, "exclude": ["*.csv", "data/prod_*"],
 "python_env": "C:\\canonical\\path\\.venv"}
```

* **Environment guardrail (every agent and check).** Secret-looking variables, database settings
  (`DATABASE_URL`, `DB_*`, `PG*`, `MYSQL*`, `MSSQL*`, `REDIS*`, …) and any variable whose value is a URL
  with a password are removed. `env` is then applied (it may not set `PATH`, `PYTHON*` loader variables,
  `LD_*`/`DYLD_*` or database settings), and the `python_env` virtualenv's `Scripts`/`bin` folder goes
  first on `PATH`. The venv must be inside the project; one made on another OS (no `Scripts`/`bin` here)
  is reported and skipped.
* **`exclude`**: files matching these patterns are never copied into the staging workspace (on top of
  `.env` files, keys and credential files, which are always left out).
* **Verify step**: an assignment with `"adapter": "verify"` runs `verify_commands` (validated like
  allowed commands, no shell) in a throwaway copy of the staging workspace, so nothing they write enters
  the change set. Every command runs even if one fails. The step succeeds when the commands could be
  run; the reply is a Markdown report (PASS/FAIL per command, the output tail of failures), and
  `stats.checks_total` / `stats.checks_failed` say whether the checks passed. Later steps get the report
  (up to 12 000 characters) in their prompt. A lineup with a Verify step needs `verify_commands`.

### `GET /tasks?project_id=` / `GET /tasks/{id}`
Task record: `id, state, objective, project, assignments[{index, role, adapter, profile, policy, status, result, error, exit_code}], workspace, changes_summary, error, created_at, updated_at, started_at, finished_at, event_count`.

States: `queued → preparing → running → awaiting_review | completed | failed | cancelled`, then `applied | rejected` after review; `interrupted` if the engine restarted mid-task.

### `GET /tasks/{id}/events?after=N&follow=1|0`
`follow=1`: Server-Sent Events. Each frame is `id: <seq>`, `event: <type>`, `data: {"seq","type","ts","payload"}`; ends with `event: end` once the task is terminal; `: keepalive` comments every 15 s. `follow=0`: `{"events":[...],"state":...}`.

Event types: `task.submitted`, `task.state`, `task.cancel_requested`, `workspace.ready`, `assignment.started`, `assignment.finished`, `agent.started`, `agent.system`, `agent.message`, `agent.thinking`, `agent.tool_use`, `agent.tool_result`, `agent.file_change`, `agent.usage`, `agent.result`, `agent.error`, `agent.stderr`, `agent.output`, `agent.raw`, `agent.finished`, `changes.ready`, `task.followup`. Payloads are secret-redacted and bounded.

### `POST /tasks/{id}/cancel`
Kills the running adapter's whole process tree. `{"cancelled": true}`.

### `POST /tasks/{id}/followup`
Continues one step's conversation with a new message, in the task's kept staging copy, so its edits join the same change set.
Body: `{"message": "...", "resume_from": <step index, default: latest step with a session>, "policy": {...}?, "read_only": bool?, "profile": "..."?, "images": [...]?}`.
Allowed from `awaiting_review | completed | failed | cancelled | interrupted` while `can_follow_up` is true. The task gets a new assignment
(`followup_of`, `message`, `resume_session`) and goes back to `queued`. 400 = bad input, 409 = not possible now (running, applied/rejected,
staging copy expired, step reported no conversation id).

Each assignment records the conversation id its CLI reports (`session_id`; Codex `thread_id`, Antigravity `conversation_id`), saved as soon as
it appears so a cancelled run can be continued. Resume commands: Claude Code `--resume <id>`, Codex `exec resume <id> -`, Antigravity
`--conversation=<id>` (agy reports it on its `init` and `result` records). A finished task keeps its staging copy until it is applied, rejected,
or idle for `CODE_STUDIO_ENGINE_WORKSPACE_RETENTION_DAYS` (default 7).

### `GET /tasks/{id}/changes`
```json
{"task_id": "...", "state": "awaiting_review", "base_revision": "...",
 "changes": [{"path": "src/a.py", "action": "added|modified|deleted", "base_sha256": "...", "new_sha256": "...",
              "binary": false, "sensitive": false, "size": 123, "diff": "unified diff",
              "base_text": "...", "new_text": "...", "content_b64": "...", "too_large": false}]}
```

### `POST /tasks/{id}/approve`
Records the IDE's decision **after** the IDE has validated and applied (or
rejected) the changes itself: `{"decision": "applied|partially_applied|rejected", "files": [...], "actor": "..."}`.
409 unless the task is `awaiting_review`. The engine never writes to the live
project; it deletes the staging workspace after the decision.

## IDE-side apply rules (`agents_client/service.py`)
1. Every path is re-validated against the canonical project root (traversal/link escapes rejected).
2. Sensitive/credential paths and `.git/` are never written.
3. Live file hash must equal the change's `base_sha256` (or be absent for `added`); any mismatch → 409, **nothing** applied.
4. A recovery snapshot of all affected paths is taken first; failure midway rolls back from it.
5. Content integrity is checked against `new_sha256` before writing (atomic writes).

## `POST /generate` — one-shot text (e.g. AI commit messages)

Body: `{"prompt": str, "adapter"?: str, "model"?: str, "timeout_seconds"?: 10..300}`.
Runs the adapter read-only, with no tools (Claude Code also skips MCP servers, skills and
its coding system prompt), in an empty scratch folder that is deleted afterwards. No task,
staging copy or review is created. Without `adapter` the engine picks the first available
of claude-code, codex, antigravity; Claude Code defaults to the `haiku` model.
Returns `{"text", "adapter", "adapter_name", "stats"}`. Errors: 400 bad body, 404 unknown
adapter, 409 adapter unavailable, 502 the CLI failed or returned nothing.

The IDE uses it from `POST /api/projects/<id>/git/commit-message`, which sends the staged
diff (or every working-tree change when nothing is staged, capped at 20,000 characters)
plus the repository's recent commit subjects, and returns `{"message", "adapter", "staged"}`.
