# Remove the local copy of Code Studio: shortcuts, program files and Python
# environment. Your data (database, settings) is kept unless you choose to
# delete it. Python, Node.js and the agent tools stay installed.
param([string]$Root, [switch]$Yes, [switch]$DeleteData)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $Root) { $Root = Get-RootFromScript $PSScriptRoot }
Set-Location $env:TEMP

$exitCode = 0
try {
    $cfg = Read-Config $Root
    if ($cfg -and (Test-LocalCopyRunning ([int]$cfg.port))) { throw 'Code Studio is running. Close its window first.' }
    Write-Host "Remove Code Studio from $Root ?" -ForegroundColor Yellow
    if (-not $Yes) {
        $ans = Read-Host '    Continue? [y/N]'
        if ($ans -notmatch '^(y|yes)$') { throw 'Cancelled.' }
        $ans = Read-Host '    Also delete your local data and settings (database, sign-in, project list)? [y/N]'
        $DeleteData = $ans -match '^(y|yes)$'
    }
    $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Code Studio'
    $desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Code Studio.lnk'
    foreach ($p in @($menu, $desktop)) { if (Test-Path $p) { Remove-Item $p -Recurse -Force } }
    if ($DeleteData) {
        Remove-Item $Root -Recurse -Force
        Write-Info 'Code Studio and its data were removed.'
    } else {
        foreach ($name in @('app', 'app.new', 'app.old', 'venv', 'codestudio.ico')) {
            $p = Join-Path $Root $name
            if (Test-Path $p) { Remove-Item $p -Recurse -Force }
        }
        Write-Info "Code Studio was removed. Your data and settings are still in $Root (installing again reuses them)."
    }
} catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
}
if (-not $Yes) { Read-Host 'Press Enter to close' | Out-Null }
exit $exitCode
