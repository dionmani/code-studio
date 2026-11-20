# Running Code Studio on Docker (Unraid)

One image (`code-studio-ide`) runs two containers. On Unraid they form the
Compose Manager stack **`code_studio_ide`** in the **Docker** tab, next to
your other stacks.

| Container | Role | Mounts | Network |
|---|---|---|---|
| `code-studio-ide` | Django/daphne IDE, terminal, Git, review/apply | projects **read-write** at `/projects`, `ide-data` → `/data` | published on `CODE_STUDIO_PORT` (8197) |
| `code-studio-engine` | Agent Engine + Claude Code / Codex / Antigravity CLIs | projects **read-only** at `/projects`, `engine-data`, `agent-home` (CLI logins) | internal Compose network only |

Agents only ever edit staging copies under `engine-data`. The read-only mount
makes it impossible for an agent to write your live files; only the IDE
applies changes, after you approve them.

## Layout on the server
```
/mnt/user/appdata/code-studio-ide/
  docker-compose.yml      the stack (copy of compose.yaml from the repo)
  .env                    your settings: port, paths, PUID/PGID, admin
  icon.png                Docker-tab icon
  src/                    source the image is built from (replaced by each deploy)
  ide-data/ ide-home/ engine-data/ agent-home/   persistent data (never touched by deploys)
/boot/config/plugins/compose.manager/projects/code_studio_ide/   stack registration + WebUI/icon labels
```

## Day-to-day: Docker tab (Compose Manager)
* **Edit settings:** click the stack's gear icon → **Edit Stack** → **ENV File**,
  save, then **Compose Up**. To change the port or address, update
  `CODE_STUDIO_PORT` together with `CODE_STUDIO_ALLOWED_HOSTS` and
  `CODE_STUDIO_CSRF_TRUSTED_ORIGINS`.
* **Compose Up / Down / Logs** work as for any stack. Autostart is on.
* **Update Stack** rebuilds the image from `src/`, recreates the containers and
  deletes the replaced image. The "pull" step it runs first is skipped or fails
  harmlessly, because the image is built locally, not pulled from a registry.
* **WebUI** (container menu) opens http://192.168.1.12:8197. **Console** opens bash.

## Shipping new code from Windows
```powershell
.\scripts\deploy_unraid.ps1        # root@192.168.1.12, projects=/mnt/user/coding, port 8197
```
It needs SSH key access as root. It uploads the source to `src/`, refreshes
`docker-compose.yml` and the stack registration, and then runs **the same
"Update Stack" command as the Docker tab**. Afterwards it prunes any dangling
images this stack left behind. One build per deploy, and no orphaned images:
each run reports `dangling images left by this stack: 0`.

`.env` is created on the first run (random admin password and service token)
and is never overwritten. New settings are appended only if missing. To read
the password:
```
ssh root@192.168.1.12 grep ADMIN_PASSWORD /mnt/user/appdata/code-studio-ide/.env
```
That value is only used to create the account on the very first start.
Change the password later with
`docker exec -it code-studio-ide python manage.py changepassword admin`.

Plain CLI on the server works too:
```
cd /mnt/user/appdata/code-studio-ide
docker compose ps | logs -f | restart | down | up -d --build
```

## Logging the agent CLIs in (once; stored in `agent-home`)
From the Docker tab (container → **Console**) or over SSH:
```
docker exec -it code-studio-engine claude                    # then /login, follow the URL
docker exec -it code-studio-engine codex login --device-auth
docker exec -it code-studio-engine agy                       # Google sign-in
```
After `agy` is logged in, an admin clicks **Turn on** next to Google Antigravity
in Settings > Agent team. It takes effect immediately, with no restart. The choice is saved in
`engine-data/engine/settings.json`. (The old `CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY`
line in `.env` only seeds that file the first time; you can delete it.)
## Permissions
* Containers run as `PUID:PGID` (default `99:100`, Unraid `nobody:users`).
  Files you create over SMB are normally writable by them. Folders owned by
  another UID without group/other write access open, but saves fail. Fix with
  Unraid **Tools → New Permissions**, or set `PUID`/`PGID` in `.env`.
* Both containers run with no Linux capabilities and `no-new-privileges`.
  There is no Docker socket and no host paths beyond the project share and
  the app's own appdata folder.
* **Engine seccomp:** Codex's per-task sandbox (bubblewrap) needs to create
  user namespaces, which Docker's default seccomp profile blocks. So the
  engine container runs with `seccomp=unconfined`. Verified result: Codex
  `workspace` mode can write only inside the task workspace, and `read-only`
  mode can write nothing. The IDE container keeps the default profile.

## Security notes
* Plain HTTP on the LAN: `CODE_STUDIO_SECURE_COOKIES=0`. If you publish it
  through a reverse proxy, use HTTPS, set it to `1`, add the public origin to
  `CODE_STUDIO_CSRF_TRUSTED_ORIGINS`, and keep it behind access control. The
  IDE includes a terminal.
* The terminal runs `bash` inside the IDE container, under the command policy
  (paths outside the project are refused). It is a policy guard, not a
  sandbox: a determined user of the terminal can read the IDE's own `/data`.
  Give accounts only to people you trust with the server.

## Verified on McRAIDFace (Unraid 6.18, Docker 29.8, Compose 2.40), 2026-11-17
* Test suite inside the image (Linux, uid 99): 119 passed, 30 skipped (Windows-only cases).
* Live: login page, fingerprinted static files and Monaco load from the LAN; anonymous API returns 401; engine port not reachable from the LAN.
* The IDE reaches the engine; the engine detects Claude Code 2.1.327, Codex 0.169.0 and agy 1.5.10.
