@echo off
cd /d "%~dp0"
python -B "%~dp0tiktok_manual_sync.py" %*
echo.
echo Manual sync session stopped. Local data and queue are retained.
pause
