@echo off
REM Start the tray host detached and hidden so no console window remains open.
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tray.ps1" %*
