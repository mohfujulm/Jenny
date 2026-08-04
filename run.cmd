@echo off
REM Compatibility wrapper: forward all arguments to the documented PowerShell runner.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
