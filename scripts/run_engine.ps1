# Start the Agent Engine on http://127.0.0.1:8765 (separate process; the IDE works without it)
#   .\scripts\run_engine.ps1                      # Claude Code + Codex (if installed and logged in)
#   Antigravity: run 'agy' once to log in, then "Turn on" in Settings > Agent team (no restart needed)
param([int]$Port = 8765, [switch]$EnableAntigravity)
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($EnableAntigravity) {
    # Legacy switch: only seeds the first value of <data>\engine\settings.json.
    Write-Host "Note: -EnableAntigravity is no longer needed. Turn Antigravity on/off in Settings > Agent team (no restart)."
    $env:CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY = '1'
}
.\.venv\Scripts\python -m agent_engine --port $Port
