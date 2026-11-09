@echo off
rem Double-click to start LFM3-Omni Lab on Windows (it runs start.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
