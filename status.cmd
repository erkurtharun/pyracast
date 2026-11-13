@echo off
setlocal
cd /d "%~dp0"
uv run --locked python -m scripts.dev status
pause
