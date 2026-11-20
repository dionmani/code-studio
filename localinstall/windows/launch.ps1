# Start the local copy of Code Studio: offer a pending update, start the agent
# engine and the IDE, open the browser. Closing this window stops both.
#
#   launch.ps1 [-Root <dir>] [-NoUpdateCheck] [-NoBrowser]
param([string]$Root, [switch]$NoUpdateCheck, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Root) { $Root = Get-RootFromScript $PSScriptRoot }
Set-Location $env:TEMP
try { $Host.UI.RawUI.WindowTitle = 'Code Studio' } catch { }

$engine = $null
$ide = $null
$exitCode = 0
try {
    $cfg = Read-Config $Root
    if (-not $cfg) { throw "No config.json in $Root. Run the installer again." }
    $port = [int]$cfg.port
    $url = "http://127.0.0.1:$port/"

    $pending = $null
    if (-not $NoUpdateCheck) { $pending = Get-PendingUpdate $Root $cfg }
    $confirmed = $false

    if (Test-LocalCopyRunning $port) {
        if (-not $pending) {
            Write-Info "Code Studio is already running at $url"
            if (-not $NoBrowser) { Start-Process $url }
            exit 0
        }
        # Already running in another window, and the server has a newer build:
        # offer to restart it here so the update can be installed.
        Write-Step "Update available: $(Format-Build $pending.Local) -> $(Format-Build $pending.Remote)"
        Write-Info 'Code Studio is already running (in its other console window).'
        Write-Info 'Restarting stops it briefly. Running agent tasks are stopped; unsaved editor changes in the browser are lost.'
        $ans = Read-Host '    Restart Code Studio now and install the update? [Y/n]'
        if ($ans -match '^(n|no)$') {
            Write-Info "Keeping the running version. Opening $url"
            if (-not $NoBrowser) { Start-Process $url }
            exit 0
        }
        Write-Info 'Stopping the running copy...'
        Stop-LocalCopy $Root $cfg
        $confirmed = $true
    }
    if (Test-PortListening $port) {
        throw "Port $port is used by another program. Close it, or set a free ""port"" in $Root\config.json."
    }

    # ---- offer an update when the server runs a different build
    if ($pending) {
        $local = $pending.Local
        $remote = $pending.Remote
        if ($remote) {
            $ans = ''
            if (-not $confirmed) {
                Write-Step "Update available: $(Format-Build $local) -> $(Format-Build $remote)"
                $ans = Read-Host '    Install it now? Your settings, projects and data are kept. [Y/n]'
            }
            if ($ans -notmatch '^(n|no)$') {
                & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile `
                    -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'update.ps1') -Root $Root -Yes
                if ($LASTEXITCODE -ne 0) { Write-Warn 'The update did not complete; starting the current version.' }
                $cfg = Read-Config $Root
            }
        }
    }

    Set-CodeStudioEnv $Root $cfg
    $py = Join-Path $Root 'venv\Scripts\python.exe'
    $app = Join-Path $Root 'app'
    Write-Step "Starting Code Studio $(Format-Build (Read-BuildInfo $app))"
    $engine = Start-Process -FilePath $py -ArgumentList @('-m', 'agent_engine', '--port', "$($cfg.engine_port)") `
        -WorkingDirectory $app -NoNewWindow -PassThru
    $ide = Start-Process -FilePath $py -ArgumentList @('-m', 'daphne', '-b', '127.0.0.1', '-p', "$port", 'codestudio.asgi:application') `
        -WorkingDirectory $app -NoNewWindow -PassThru

    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-PortListening $port)) {
        if ($ide.HasExited) { throw "Code Studio stopped while starting (exit code $($ide.ExitCode)). See the messages above." }
        if ((Get-Date) -gt $deadline) { throw 'Code Studio did not start within 90 seconds.' }
        Start-Sleep -Milliseconds 500
    }
    Write-Host ''
    Write-Host "Code Studio is running at $url" -ForegroundColor Green
    Write-Info 'Keep this window open while you work. Close it (or press Ctrl+C) to stop Code Studio.'
    if (-not $NoBrowser -and $cfg.open_browser -ne $false) { Start-Process $url }
    Wait-Process -Id $ide.Id
} catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
} finally {
    Stop-ProcessTree $engine
    Stop-ProcessTree $ide
}
if ($exitCode -ne 0) { Read-Host 'Press Enter to close' | Out-Null }
exit $exitCode
