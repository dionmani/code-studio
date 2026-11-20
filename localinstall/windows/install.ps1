# Code Studio for Windows: install, or update an existing install in place.
#
# Note for edits: Windows PowerShell 5.1 strips double quotes inside arguments
# passed to programs, so inline Python (-c) must not contain any.
#
# Normally started by "Install Code Studio.cmd" (fresh download) or by update.ps1.
# An update replaces only app\ and syncs venv\; data\ and config.json are kept,
# and the database is backed up before migrations run. A failed update puts the
# previous version (and database) back.
#
#   install.ps1 -Package <extracted zip folder> [-Root <dir>] [-Update]
#   Unattended: -AdminUser admin -AdminPassword <pw> -ProjectsFolder <dir|none> -AgentTools no -NoShortcuts -NoLaunch
param(
    [string]$Package,
    [string]$Root,
    [switch]$Update,
    [string]$AdminUser,
    [string]$AdminPassword,
    [string]$ProjectsFolder,
    [ValidateSet('ask', 'yes', 'no')][string]$AgentTools = 'ask',
    [switch]$NoShortcuts,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $Package) { $Package = Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent }
if (-not $Root) { $Root = Get-DefaultRoot }
$Package = (Resolve-Path $Package).Path
Set-Location $env:TEMP   # never hold a handle on the folders being swapped

$app = Join-Path $Root 'app'
$staged = Join-Path $Root 'app.new'
$previous = Join-Path $Root 'app.old'
$venv = Join-Path $Root 'venv'
$py = Join-Path $venv 'Scripts\python.exe'
$data = Join-Path $Root 'data'
$logs = Join-Path $Root 'logs'
$db = Join-Path $data 'db.sqlite3'

function Find-Python312 {
    $exe = Get-NativeOutput 'py' @('-3.12', '-c', 'import sys; print(sys.executable)')
    if ($exe -and (Test-Path $exe)) { return $exe }
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    $ver = Get-NativeOutput 'python' @('-c', 'import sys; print(*sys.version_info[:2], sep=chr(46))')
    if ($ver -eq '3.12') { return (Get-NativeOutput 'python' @('-c', 'import sys; print(sys.executable)')) }
    return $null
}

function Install-WithWinget([string]$Id, [string]$Name, [string[]]$Extra) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$Name is needed but winget isn't available to install it. Install $Name yourself, then run this installer again."
    }
    Write-Info "Installing $Name with winget (this can take a few minutes)..."
    $wingetArgs = @('install', '--id', $Id, '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements') + $Extra
    Invoke-Native "Installing $Name" 'winget' $wingetArgs
    Update-SessionPath
}

function Read-NewPassword {
    while ($true) {
        $a = Read-Host '    Choose a password (8+ characters)' -AsSecureString
        $b = Read-Host '    Type it again' -AsSecureString
        $pa = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
        $pb = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
        if ($pa.Length -lt 8) { Write-Warn 'Use at least 8 characters.'; continue }
        if ($pa -ne $pb) { Write-Warn 'The two passwords differ, try again.'; continue }
        return $pa
    }
}

function Read-ProjectsFolder {
    $default = Join-Path $env:USERPROFILE 'Documents'
    while ($true) {
        $ans = Read-Host "    Folder that holds your projects [$default] (or - to skip)"
        if (-not $ans) { $ans = $default }
        if ($ans -eq '-') { return '' }
        $ans = $ans.Trim('"')
        if (Test-Path $ans -PathType Container) { return (Resolve-Path $ans).Path }
        Write-Warn "Folder not found: $ans"
    }
}

function New-IconFile([string]$Png, [string]$Ico) {
    # A single-image .ico that embeds the PNG (supported since Windows Vista).
    $bytes = [IO.File]::ReadAllBytes($Png)
    $w = [int]$bytes[19] + 256 * [int]$bytes[18]
    $h = [int]$bytes[23] + 256 * [int]$bytes[22]
    $ms = New-Object IO.MemoryStream
    $bw = New-Object IO.BinaryWriter($ms)
    $bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]1)
    $bw.Write([Byte]($w % 256)); $bw.Write([Byte]($h % 256)); $bw.Write([Byte]0); $bw.Write([Byte]0)
    $bw.Write([UInt16]1); $bw.Write([UInt16]32); $bw.Write([UInt32]$bytes.Length); $bw.Write([UInt32]22)
    $bw.Write($bytes); $bw.Flush()
    [IO.File]::WriteAllBytes($Ico, $ms.ToArray())
}

function New-Shortcuts {
    $ico = Join-Path $Root 'codestudio.ico'
    New-IconFile (Join-Path $app 'static\ide\icon.png') $ico
    $scripts = Join-Path $app 'localinstall\windows'
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shell = New-Object -ComObject WScript.Shell
    $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Code Studio'
    New-Item -ItemType Directory -Force $menu | Out-Null
    $items = @(
        @{ Path = (Join-Path $menu 'Code Studio.lnk'); Script = 'launch.ps1'; Extra = ''; Desc = 'Start Code Studio' },
        @{ Path = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Code Studio.lnk'); Script = 'launch.ps1'; Extra = ''; Desc = 'Start Code Studio' },
        @{ Path = (Join-Path $menu 'Update Code Studio.lnk'); Script = 'update.ps1'; Extra = ' -Pause'; Desc = 'Update Code Studio from its server' },
        @{ Path = (Join-Path $menu 'Uninstall Code Studio.lnk'); Script = 'uninstall.ps1'; Extra = ''; Desc = 'Remove Code Studio from this PC' }
    )
    foreach ($i in $items) {
        $s = $shell.CreateShortcut($i.Path)
        $s.TargetPath = $ps
        $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $scripts $i.Script)`"$($i.Extra)"
        $s.WorkingDirectory = $Root
        $s.IconLocation = "$ico,0"
        $s.Description = $i.Desc
        $s.Save()
    }
    Write-Info "Shortcuts: Desktop and Start Menu > Code Studio"
}

function Invoke-Pip([string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $all = New-Object System.Collections.Generic.List[string]
    try {
        & $py -m pip @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            $all.Add($line)
            if ($line -match '^(Collecting|Downloading|Installing collected|Successfully installed)') {
                Write-Host ("      " + ($line -replace '\s+\(from .*\)$', ''))
            }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) {
        $all | Select-Object -Last 15 | ForEach-Object { Write-Host "      $_" }
        throw "Installing packages failed (exit code $code). The lines above show why."
    }
}

function Backup-Database([string]$Label) {
    if (-not (Test-Path $db)) { return $null }
    $dir = Join-Path $data 'backups'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $copy = Join-Path $dir ("db-{0}-{1}.sqlite3" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $Label)
    Copy-Item $db $copy
    Get-ChildItem $dir -Filter 'db-*.sqlite3' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
    return $copy
}

function Invoke-Manage([string]$What, [string[]]$Arguments) {
    Push-Location $app
    try { Invoke-Native $What $py (@('manage.py') + $Arguments) } finally { Pop-Location }
}

New-Item -ItemType Directory -Force $Root, $data, $logs | Out-Null
$log = Join-Path $logs ("install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Start-Transcript -Path $log | Out-Null
$exitCode = 0
try {
    $infoPath = Join-Path $Package 'install-info.json'
    $source = Join-Path $Package 'app'
    if (-not (Test-Path $infoPath) -or -not (Test-Path (Join-Path $source 'manage.py'))) {
        throw "This doesn't look like an extracted Code Studio package: $Package"
    }
    $info = Get-Content $infoPath -Raw | ConvertFrom-Json
    $existing = Test-Path (Join-Path $app 'manage.py')
    $oldBuild = Read-BuildInfo $app

    Write-Host ''
    Write-Host "Code Studio $($info.version) ($($info.build)) for Windows" -ForegroundColor Green
    Write-Info "Install folder: $Root"
    if ($existing) { Write-Info "Updating $(Format-Build $oldBuild). Your settings, projects and data are kept." }

    # ---- config.json: keep everything; add missing keys; refresh the server connection
    $cfg = Read-Config $Root
    if (-not $cfg) { $cfg = New-Object PSObject }
    $defaults = [ordered]@{ port = 0; engine_port = 0; check_updates = $true; open_browser = $true; shortcuts = (-not $NoShortcuts)
                            server = ''; token = '' }
    foreach ($k in $defaults.Keys) {
        if (-not ($cfg.PSObject.Properties.Name -contains $k)) { $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $defaults[$k] }
    }
    if ($NoShortcuts) { $cfg.shortcuts = $false }   # an update keeps the original choice
    if ($info.server) { $cfg.server = $info.server }
    if ($info.token) { $cfg.token = $info.token }
    if ([int]$cfg.port -le 0) { $cfg.port = Find-FreePort 8000 }
    if ([int]$cfg.engine_port -le 0) { $cfg.engine_port = Find-FreePort 8765 }

    if (Test-LocalCopyRunning ([int]$cfg.port)) {
        throw "Code Studio is running (http://127.0.0.1:$($cfg.port)). Close its window, then run this again."
    }

    # ---- Python 3.12
    Write-Step 'Checking Python 3.12'
    $basePython = Find-Python312
    if (-not $basePython) {
        Install-WithWinget 'Python.Python.3.12' 'Python 3.12' @('--scope', 'user')
        $basePython = Find-Python312
        if (-not $basePython) { throw 'Python 3.12 was installed but could not be found. Open a new window and run the installer again.' }
    }
    Write-Info "Using $basePython"

    # ---- stage the new program files next to the current ones
    Write-Step 'Copying program files'
    if (Test-Path $staged) { Remove-Item $staged -Recurse -Force }
    if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }
    $files = @(Get-ChildItem $source -Recurse -File)
    Write-Info "$($files.Count) files..."
    Copy-Item $source $staged -Recurse
    Write-Info 'Clearing the "downloaded from the internet" mark so Windows does not block them...'
    Get-ChildItem $staged -Recurse -File | Unblock-File

    # ---- Python environment (kept between updates; rebuilt if broken)
    Write-Step 'Preparing the Python environment'
    if ((Test-Path $venv) -and ((Get-NativeOutput $py @('-c', 'import sys; print(*sys.version_info[:2], sep=chr(46))')) -ne '3.12')) {
        Write-Warn 'The existing environment is broken or on another Python version: rebuilding it.'
        Remove-Item $venv -Recurse -Force
    }
    if (-not (Test-Path $py)) { Invoke-Native 'Creating the Python environment' $basePython @('-m', 'venv', $venv) }
    $reqs = Join-Path $logs 'requirements.runtime.txt'
    # Test-only packages are not needed to run Code Studio (same filter as the Dockerfile).
    Get-Content (Join-Path $staged 'requirements.txt') | Where-Object { $_ -notmatch '^(pytest|playwright)' } |
        Set-Content $reqs -Encoding ASCII
    Invoke-Native 'Updating pip' $py @('-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', '--quiet', 'pip') -Quiet
    Write-Info 'Installing packages (first install takes a minute or two; updates are quicker)...'
    # pip's own progress lines, minus the "already satisfied" noise, so slow downloads don't look like a hang.
    Invoke-Pip @('install', '--disable-pip-version-check', '--progress-bar', 'off', '-r', $reqs)

    # ---- swap in the new version, then migrate; put everything back if that fails
    Write-Step 'Installing the new version'
    Set-CodeStudioEnv $Root $cfg
    $backup = Backup-Database ("before-" + $info.build)
    if ($backup) { Write-Info "Database backed up to $backup" }
    try {
        if ($existing) { Move-Item $app $previous }
        Move-Item $staged $app
        Write-Info 'Preparing the web files...'
        Invoke-Manage 'Preparing static files' @('collectstatic', '--noinput', '-v', '0', '--clear')
        Write-Info 'Updating the database...'
        Invoke-Manage 'Updating the database' @('migrate', '--noinput', '-v', '0')
    } catch {
        if (Test-Path $previous) {
            Write-Warn 'Install step failed: restoring the previous version and database.'
            if (Test-Path $app) { Remove-Item $app -Recurse -Force }
            Move-Item $previous $app
            if ($backup) { Copy-Item $backup $db -Force }
        }
        throw
    }
    if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }
    Save-Config $Root $cfg

    # ---- first run: admin account and projects folder
    $hasUsers = Get-NativeOutput $py @((Join-Path $app 'manage.py'), 'shell', '-v', '0', '-c',
        'from django.contrib.auth import get_user_model as U; print(U().objects.exists())')
    if ($hasUsers -match 'True') {
        Invoke-Manage 'Checking default agent profiles' @('bootstrap', '--first-run-only')
    } else {
        Write-Step 'Create the admin account for this local copy'
        if (-not $AdminUser) {
            $AdminUser = Read-Host '    Username [admin]'
            if (-not $AdminUser) { $AdminUser = 'admin' }
        }
        if (-not $AdminPassword) { $AdminPassword = Read-NewPassword }
        if (-not $PSBoundParameters.ContainsKey('ProjectsFolder')) { $ProjectsFolder = Read-ProjectsFolder }
        if (@('-', 'none') -contains $ProjectsFolder) { $ProjectsFolder = '' }
        $env:CODE_STUDIO_BOOTSTRAP_PASSWORD = $AdminPassword   # not on the command line
        try {
            Invoke-Manage 'Creating the admin account' @('bootstrap', '--first-run-only', '--username', $AdminUser)
        } finally {
            Remove-Item Env:CODE_STUDIO_BOOTSTRAP_PASSWORD
        }
        if ($ProjectsFolder) {
            # A folder Code Studio refuses (system or AppData folders, a drive root...) is not fatal.
            try {
                Invoke-Manage 'Adding the projects folder' @('bootstrap', '--username', $AdminUser, '--root', "projects=$ProjectsFolder")
            } catch {
                Write-Warn "Could not add $ProjectsFolder as a projects folder (see above). Add one later in the Projects tab."
            }
        }
    }

    # ---- optional: AI agent command-line tools (fresh installs only)
    if (-not $Update -and -not $existing) {
        if ($AgentTools -eq 'ask') {
            $ans = Read-Host '    Install the Claude Code and Codex command-line tools for AI agents? [y/N]'
            if ($ans -match '^(y|yes)$') { $AgentTools = 'yes' } else { $AgentTools = 'no' }
        }
        if ($AgentTools -eq 'yes') {
            Write-Step 'Installing the AI agent tools'
            try {
                if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { Install-WithWinget 'OpenJS.NodeJS.LTS' 'Node.js' @() }
                Invoke-Native 'Installing Claude Code and Codex' 'npm.cmd' @('install', '-g', '@anthropic-ai/claude-code', '@openai/codex')
                Write-Info "Sign in once before using agents: run 'claude' and 'codex login' in a new terminal."
            } catch {
                Write-Warn "Skipped the agent tools: $($_.Exception.Message) Code Studio works without them."
            }
        }
    }

    if ($cfg.shortcuts -ne $false) { Write-Step 'Creating shortcuts'; New-Shortcuts }

    $newBuild = Read-BuildInfo $app
    Write-Host ''
    Write-Host "Code Studio $(Format-Build $newBuild) is installed." -ForegroundColor Green
    Write-Info "It runs at http://127.0.0.1:$($cfg.port)/ once started. Log: $log"

    if (-not $NoLaunch -and -not $Update) {
        $ans = Read-Host '    Start Code Studio now? [Y/n]'
        if ($ans -notmatch '^(n|no)$') {
            Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
                -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $app 'localinstall\windows\launch.ps1')`"") `
                -WorkingDirectory $Root
        }
    }
} catch {
    Write-Fail $_.Exception.Message
    Write-Info "Log: $log"
    $exitCode = 1
} finally {
    if (Test-Path $staged) { Remove-Item $staged -Recurse -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
}
exit $exitCode
