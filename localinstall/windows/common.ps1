# Shared helpers for the Code Studio Windows scripts (dot-sourced).
# Keep this file ASCII-only and Windows PowerShell 5.1 compatible: 5.1 reads
# BOM-less scripts in the ANSI code page and has no ?? / ?: / && operators.
#
# Install layout (Root = %LOCALAPPDATA%\Programs\CodeStudio):
#   app\          program files            replaced on every update
#   venv\         Python environment       kept; packages synced on update
#   data\         database, secrets, data  never touched by updates (backed up first)
#   config.json   local settings           kept; only missing keys are added
#   logs\         install / update logs

function Get-DefaultRoot { Join-Path $env:LOCALAPPDATA 'Programs\CodeStudio' }

# Scripts live in <Root>\app\localinstall\windows
function Get-RootFromScript([string]$ScriptDir) {
    Split-Path (Split-Path (Split-Path $ScriptDir -Parent) -Parent) -Parent
}

function Write-Step([string]$Message) { Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-Warn([string]$Message) { Write-Host "    $Message" -ForegroundColor Yellow }
function Write-Fail([string]$Message) { Write-Host ''; Write-Host "ERROR: $Message" -ForegroundColor Red }

# Run a program and throw on a non-zero exit code. Native stderr output is not
# treated as an error (pip writes warnings there); Windows PowerShell 5.1 would
# otherwise raise on it when $ErrorActionPreference is 'Stop'.
function Invoke-Native {
    param([string]$What, [string]$FilePath, [string[]]$Arguments, [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = @()
    try {
        if ($Quiet) {
            $lines = @(& $FilePath @Arguments 2>&1 | ForEach-Object { "$_" })
        } else {
            & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) {
        foreach ($l in $lines) { Write-Host "    $l" }
        throw "$What failed (exit code $code)."
    }
}

# Output of a program, or $null if it is missing or fails.
function Get-NativeOutput {
    param([string]$FilePath, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $FilePath @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($out | Out-String).Trim())
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
}

# Pick up PATH changes made by winget installs in this session.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Read-Config([string]$Root) {
    $path = Join-Path $Root 'config.json'
    if (-not (Test-Path $path)) { return $null }
    return (Get-Content $path -Raw | ConvertFrom-Json)
}

function Save-Config([string]$Root, $Config) {
    $json = $Config | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $Root 'config.json'), $json, (New-Object Text.UTF8Encoding($false)))
}

function Read-BuildInfo([string]$AppDir) {
    $path = Join-Path $AppDir 'BUILD_INFO.json'
    if (-not (Test-Path $path)) { return $null }
    return (Get-Content $path -Raw | ConvertFrom-Json)
}

# {version, build} the server runs, or $null if it can't be reached.
function Get-RemoteBuild([string]$Server) {
    if (-not $Server) { return $null }
    try { return (Invoke-RestMethod -Uri "$Server/api/version" -TimeoutSec 5 -UseBasicParsing) } catch { return $null }
}

function Format-Build($Info) {
    if (-not $Info) { return 'unknown' }
    return "$($Info.version) ($($Info.build))"
}

function Test-PortListening([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($pending.AsyncWaitHandle.WaitOne(300)) { $client.EndConnect($pending); return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# True when a local copy of Code Studio answers on this port.
function Test-LocalCopyRunning([int]$Port) {
    if (-not (Test-PortListening $Port)) { return $false }
    try {
        $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/version" -TimeoutSec 3 -UseBasicParsing
        return [bool]$v.local_install
    } catch {
        return $false
    }
}

function Find-FreePort([int]$Start) {
    for ($p = $Start; $p -lt ($Start + 50); $p++) {
        if (-not (Test-PortListening $p)) { return $p }
    }
    throw "No free port found between $Start and $($Start + 49)."
}

# Environment for every Code Studio process of a local copy (server, engine, manage.py).
function Set-CodeStudioEnv([string]$Root, $Config) {
    Remove-Item Env:DJANGO_SETTINGS_MODULE -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:PYTHONUTF8 = '1'
    $env:CODE_STUDIO_DATA_DIR = Join-Path $Root 'data'
    $env:CODE_STUDIO_DEBUG = '0'
    $env:CODE_STUDIO_SECURE_COOKIES = '0'     # plain http://127.0.0.1
    $env:CODE_STUDIO_LOCAL_INSTALL = '1'
    $env:CODE_STUDIO_OFFER_LOCAL_INSTALL = '0'
    $env:CODE_STUDIO_ALLOWED_HOSTS = '127.0.0.1,localhost'
    $env:CODE_STUDIO_ENGINE_URL = "http://127.0.0.1:$($Config.engine_port)"
    $env:CODE_STUDIO_ENGINE_PORT = "$($Config.engine_port)"
    if ($Config.server) { $env:CODE_STUDIO_UPDATE_SERVER = "$($Config.server)" }
}

# {version, build} the server offers when it differs from this copy's build, else $null.
function Get-PendingUpdate([string]$Root, $Config) {
    if ($Config.check_updates -eq $false -or -not $Config.server) { return $null }
    $local = Read-BuildInfo (Join-Path $Root 'app')
    $remote = Get-RemoteBuild $Config.server
    if ($remote -and $remote.build -and (-not $local -or $remote.build -ne $local.build)) {
        return @{ Local = $local; Remote = $remote }
    }
    return $null
}

# Stop a running local copy (the IDE and engine servers started by launch.ps1).
# Only processes listening on this copy's ports whose command line is Code
# Studio's server are stopped; the launcher window that owns them then closes.
function Stop-LocalCopy([string]$Root, $Config) {
    $targets = @(
        @{ Port = [int]$Config.port; Pattern = 'codestudio\.asgi' },
        @{ Port = [int]$Config.engine_port; Pattern = 'agent_engine' }
    )
    foreach ($t in $targets) {
        $conns = @(Get-NetTCPConnection -LocalPort $t.Port -State Listen -ErrorAction SilentlyContinue)
        foreach ($c in $conns) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($c.OwningProcess)" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -match $t.Pattern) {
                $old = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try { & taskkill.exe /PID $proc.ProcessId /T /F 2>&1 | Out-Null } finally { $ErrorActionPreference = $old }
            }
        }
    }
    $deadline = (Get-Date).AddSeconds(20)
    while ((Test-PortListening ([int]$Config.port)) -or (Test-PortListening ([int]$Config.engine_port))) {
        if ((Get-Date) -gt $deadline) { throw 'Code Studio did not stop. Close its console window, then try again.' }
        Start-Sleep -Milliseconds 500
    }
}

function Stop-ProcessTree($Process) {
    if ($Process -and -not $Process.HasExited) {
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null } finally { $ErrorActionPreference = $old }
    }
}
