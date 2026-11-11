@echo off
setlocal
cd /d "%~dp0"
uv sync --locked
if errorlevel 1 goto failed
uv run --locked python -m scripts.dev start
if errorlevel 1 goto failed
if /I not "%~1"=="--no-browser" start "" "http://127.0.0.1:3000"
exit /b 0
:failed
echo Startup failed. Check the message above and .dev logs.
pause
exit /b 1
