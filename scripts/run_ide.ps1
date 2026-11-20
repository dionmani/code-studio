# Start the Django IDE (ASGI via daphne's runserver: HTTP + WebSockets) on http://127.0.0.1:8888
param([string]$Bind = '127.0.0.1:8888')
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python manage.py runserver $Bind
