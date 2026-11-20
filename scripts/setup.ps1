# One-time setup: venv, dependencies, database, admin user + first allowlisted root.
#   .\scripts\setup.ps1 -AdminPassword 'choose-a-strong-one' -Root 'D:\Projects'
param(
    [Parameter(Mandatory = $true)][string]$AdminPassword,
    [string]$AdminUser = 'admin',
    [string]$Root = ''
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate --noinput
$bootstrapArgs = @('manage.py', 'bootstrap', '--username', $AdminUser, '--password', $AdminPassword)
if ($Root) { $bootstrapArgs += @('--root', "projects=$Root") }
.\.venv\Scripts\python @bootstrapArgs
Write-Host "Setup complete. Start the engine with scripts\run_engine.ps1 and the IDE with scripts\run_ide.ps1"
