# Update the local copy from the server it was installed from.
# Downloads the server's current package with this copy's download token and
# runs that package's installer in update mode: app\ is replaced, venv\ synced,
# data\ and config.json kept (database backed up first).
#
#   update.ps1 [-Root <dir>] [-Yes] [-Force] [-Pause]
param([string]$Root, [switch]$Yes, [switch]$Force, [switch]$Pause, [switch]$Relocated)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Root) { $Root = Get-RootFromScript $PSScriptRoot }
Set-Location $env:TEMP

# app\ is about to be replaced, so run from a temporary copy of these scripts.
if (-not $Relocated) {
    $runner = Join-Path $env:TEMP ('CodeStudio-updater-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force $runner | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'update.ps1'), (Join-Path $PSScriptRoot 'common.ps1') $runner
    $forward = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $runner 'update.ps1'), '-Root', $Root, '-Relocated')
    if ($Yes) { $forward += '-Yes' }
    if ($Force) { $forward += '-Force' }
    if ($Pause) { $forward += '-Pause' }
    & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') @forward
    $code = $LASTEXITCODE
    Remove-Item $runner -Recurse -Force -ErrorAction SilentlyContinue
    exit $code
}

$work = $null
$exitCode = 0
try {
    $cfg = Read-Config $Root
    if (-not $cfg) { throw "No Code Studio install found in $Root." }
    if (-not $cfg.server -or -not $cfg.token) {
        throw 'This copy has no update server. Download the package from your Code Studio server (Run Locally) and run its installer.'
    }
    if (Test-LocalCopyRunning ([int]$cfg.port)) {
        if ($Yes) { throw 'Code Studio is running. Close its window, then run the update again.' }
        Write-Info 'Code Studio is running. Running agent tasks are stopped; unsaved editor changes in the browser are lost.'
        $ans = Read-Host '    Stop it now so the update can be installed? [Y/n]'
        if ($ans -match '^(n|no)$') { throw 'Update cancelled. Close Code Studio, then run the update again.' }
        Stop-LocalCopy $Root $cfg
        Write-Info 'Stopped. Start Code Studio again from its shortcut after the update.'
    }

    Write-Step "Checking $($cfg.server) for updates"
    $local = Read-BuildInfo (Join-Path $Root 'app')
    $remote = Get-RemoteBuild $cfg.server
    if (-not $remote) { throw "Could not reach $($cfg.server). Is the server running and is this PC on the same network?" }
    Write-Info "This copy:  $(Format-Build $local)"
    Write-Info "Server:     $(Format-Build $remote)"
    if (-not $Force -and $local -and $remote.build -eq $local.build) {
        Write-Host '    Already up to date.' -ForegroundColor Green
    } else {
        if (-not $Yes) {
            $ans = Read-Host '    Install the update? Your settings, projects and data are kept. [Y/n]'
            if ($ans -match '^(n|no)$') { throw 'Update cancelled.' }
        }
        Write-Step 'Downloading the update'
        $work = Join-Path $env:TEMP ('CodeStudio-update-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Force $work | Out-Null
        $zip = Join-Path $work 'package.zip'
        try {
            Invoke-WebRequest -Uri "$($cfg.server)/local/download" -Headers @{ Authorization = "Bearer $($cfg.token)" } `
                -OutFile $zip -UseBasicParsing -TimeoutSec 600
        } catch {
            $status = 0
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            if ($status -eq 401) {
                throw "The server no longer accepts this copy's download token. Open $($cfg.server)/local/, download the package again and run its installer (your settings are kept)."
            }
            throw "Download failed: $($_.Exception.Message)"
        }
        Expand-Archive -Path $zip -DestinationPath (Join-Path $work 'pkg')
        $installer = Join-Path $work 'pkg\app\localinstall\windows\install.ps1'
        if (-not (Test-Path $installer)) { throw 'The downloaded package is incomplete.' }

        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -ExecutionPolicy Bypass `
            -File $installer -Package (Join-Path $work 'pkg') -Root $Root -Update
        if ($LASTEXITCODE -ne 0) { throw 'The update did not install. The previous version is still in place.' }
    }
} catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
} finally {
    if ($work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}
if ($Pause) { Read-Host 'Press Enter to close' | Out-Null }
exit $exitCode
