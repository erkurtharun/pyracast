@echo off
setlocal
cd /d "%~dp0"
uv run --locked python -m scripts.dev stop
if errorlevel 1 goto failed
exit /b 0
:failed
pause
exit /b 1
