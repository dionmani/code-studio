# Upload Code Studio to an Unraid server and update its Compose Manager stack (SSH key auth).
#   .\scripts\deploy_unraid.ps1                       # defaults: root@192.168.1.12, /mnt/user/coding
#   .\scripts\deploy_unraid.ps1 -HostName 192.168.1.12 -Projects /mnt/user/coding -Port 8197
#
# Server layout (a normal Compose Manager stack, also editable from the Docker tab):
#   /mnt/user/appdata/code-studio-ide/docker-compose.yml   stack file (copied from compose.yaml)
#   /mnt/user/appdata/code-studio-ide/.env                 settings (created once; never overwritten)
#   /mnt/user/appdata/code-studio-ide/src/                 build context (replaced on each deploy)
#   /mnt/user/appdata/code-studio-ide/{ide-data,ide-home,engine-data,agent-home}/   persistent data
#   /boot/config/plugins/compose.manager/projects/code_studio_ide/                  stack registration
# The update runs Compose Manager's own "update" (build, recreate, remove the replaced
# image), then prunes any dangling images left by this stack's builds.
param(
    [string]$HostName = '192.168.1.12',
    [string]$User = 'root',
    [string]$Projects = '/mnt/user/coding',
    [string]$AppData = '/mnt/user/appdata/code-studio-ide',
    [int]$Port = 8197
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$target = "$User@$HostName"
$archive = Join-Path ([IO.Path]::GetTempPath()) 'code-studio-src.tar.gz'

Write-Host "Packing source..."
# Windows' own bsdtar by full path: from Git Bash, "tar.exe" on PATH is GNU tar, which reads "C:\..." as host "C"
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path $tar)) { $tar = 'tar.exe' }
& $tar -czf $archive -C $repo --exclude=.venv --exclude=.e2e --exclude=.pytest_tmp --exclude=.pytest_cache `
    --exclude=__pycache__ --exclude=.env --exclude='*.sqlite3' --exclude=staticfiles .
if ($LASTEXITCODE) { throw "tar failed" }

Write-Host "Uploading to ${target}:$AppData ..."
& ssh -o BatchMode=yes $target "mkdir -p '$AppData'"
& scp -q -o BatchMode=yes $archive "${target}:$AppData/src.tar.gz"
if ($LASTEXITCODE) { throw "upload failed" }
Remove-Item $archive

$remote = @'
set -euo pipefail
A="__APPDATA__"; NAME=code_studio_ide; CM=/usr/local/emhttp/plugins/compose.manager
PROJ=/boot/config/plugins/compose.manager/projects/$NAME
cd "$A"
rm -rf src.new && mkdir src.new && tar -xzf src.tar.gz -C src.new && rm src.tar.gz
find src.new -name '*.sh' -exec sed -i 's/\r$//' {} +

# ---- settings (.env): create once, then only add missing keys -------------
if [ ! -f .env ]; then
  ( umask 077
    sed -e "s#^CODE_STUDIO_PROJECTS=.*#CODE_STUDIO_PROJECTS=__PROJECTS__#" \
        -e "s#^CODE_STUDIO_APPDATA=.*#CODE_STUDIO_APPDATA=$A#" \
        -e "s#^CODE_STUDIO_PORT=.*#CODE_STUDIO_PORT=__PORT__#" \
        -e "s#^CODE_STUDIO_ALLOWED_HOSTS=.*#CODE_STUDIO_ALLOWED_HOSTS=__HOST__,$(hostname),localhost#" \
        -e "s#^CODE_STUDIO_CSRF_TRUSTED_ORIGINS=.*#CODE_STUDIO_CSRF_TRUSTED_ORIGINS=http://__HOST__:__PORT__,http://$(hostname):__PORT__#" \
        -e "s#^CODE_STUDIO_ADMIN_PASSWORD=.*#CODE_STUDIO_ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')#" \
        -e "s#^CODE_STUDIO_ENGINE_TOKEN=.*#CODE_STUDIO_ENGINE_TOKEN=$(openssl rand -hex 32)#" \
        src.new/.env.example > .env )
  echo "created $A/.env"
fi
grep -q '^CODE_STUDIO_SRC=' .env || printf '\n# Build context for the stack (uploaded source)\nCODE_STUDIO_SRC=./src\n' >> .env
chmod 600 .env
set -a; . ./.env; set +a

for d in ide-data ide-home engine-data agent-home; do mkdir -p "$d"; done
chown -R "$PUID:$PGID" ide-data ide-home engine-data agent-home
chmod 750 ide-data ide-home engine-data agent-home

rm -rf src.old; [ -d src ] && mv src src.old; mv src.new src
rm -f src/.env compose.yaml docker-compose.yml.new
cp src/compose.yaml docker-compose.yml
cp src/static/ide/icon.png icon.png
rm -f deploy.sh deploy.log   # from older deploy versions

# ---- one-time migration from the pre-stack project name --------------------
if [ -n "$(docker ps -aq --filter label=com.docker.compose.project=code-studio-ide)" ]; then
  echo "migrating containers from project code-studio-ide -> $NAME"
  docker compose -p code-studio-ide down --remove-orphans
fi

# ---- register with Compose Manager (Docker tab) ---------------------------
if [ -d "$CM" ]; then
  mkdir -p "$PROJ"
  echo "$NAME" > "$PROJ/name"
  printf '%s' "$A" > "$PROJ/indirect"
  [ -f "$PROJ/autostart" ] || echo true > "$PROJ/autostart"
  cat > "$PROJ/docker-compose.override.yml" <<YML
services:
  ide:
    labels:
      net.unraid.docker.managed: 'composeman'
      net.unraid.docker.icon: '$A/icon.png'
      net.unraid.docker.webui: 'http://__HOST__:$CODE_STUDIO_PORT'
      net.unraid.docker.shell: 'bash'
  engine:
    labels:
      net.unraid.docker.managed: 'composeman'
      net.unraid.docker.icon: '$A/icon.png'
      net.unraid.docker.shell: 'bash'
YML
  # exactly what the Docker tab's "Update Stack" runs
  "$CM/scripts/compose.sh" -c update -p "$NAME" -d "$A" -f "$PROJ/docker-compose.override.yml" \
    2>&1 | grep -v -E 'pull access denied|failed to resolve reference|Pulling|Pulled|^ *(ide|engine) +(Skipped|Warning)' || true
else
  docker compose -p "$NAME" up -d --build
fi

# ---- remove images orphaned by this stack's rebuilds ----------------------
docker image prune -f --filter "label=com.docker.compose.project=$NAME" >/dev/null
docker image prune -f --filter "label=com.docker.compose.project=code-studio-ide" >/dev/null
rm -rf src.old
docker compose -p "$NAME" ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
for c in code-studio-ide code-studio-engine; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || { echo "ERROR: $c is not running"; exit 1; }
done
echo "dangling images left by this stack: $(docker images -qf dangling=true -f label=com.docker.compose.project=$NAME | wc -l)"
'@
$remote = $remote.Replace('__APPDATA__', $AppData).Replace('__PROJECTS__', $Projects).Replace('__PORT__', "$Port").Replace('__HOST__', $HostName)
$remote = $remote -replace "`r", ''
$script = Join-Path ([IO.Path]::GetTempPath()) 'code-studio-deploy.sh'
[IO.File]::WriteAllText($script, $remote)   # UTF-8 without BOM, LF only
& scp -q -o BatchMode=yes $script "${target}:/tmp/code-studio-deploy.sh"
Remove-Item $script
& ssh -o BatchMode=yes $target "bash /tmp/code-studio-deploy.sh > /tmp/code-studio-deploy.log 2>&1; rc=`$?; tail -n 30 /tmp/code-studio-deploy.log; rm -f /tmp/code-studio-deploy.sh; exit `$rc"
if ($LASTEXITCODE) { throw "remote deploy failed (full log: /tmp/code-studio-deploy.log on $HostName)" }
Write-Host "`nCode Studio: http://${HostName}:$Port   (Docker tab -> Compose stack 'code_studio_ide')"
Write-Host "Admin password: ssh $target grep ADMIN_PASSWORD $AppData/.env"
