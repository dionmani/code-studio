@echo off
rem Code Studio for Windows installer. Run it from the extracted package folder.
setlocal
set "HERE=%~dp0"
title Code Studio installer
if not exist "%HERE%app\localinstall\windows\install.ps1" (
  echo.
  echo   The installer files were not found next to this file.
  echo.
  echo   Right-click the downloaded zip, choose "Extract All...", open the
  echo   extracted folder and run "Install Code Studio.cmd" from there.
  echo.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%app\localinstall\windows\install.ps1" -Package "%HERE%."
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
