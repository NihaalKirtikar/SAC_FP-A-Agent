@echo off
title SAC FP^&A Agent
cd /d "%~dp0"
echo.
echo   Starting SAC FP^&A Agent ...
echo   URL: http://localhost:8502
echo   (Keep this window open. Close it to stop the app.)
echo.
rem Prefer the local self-contained venv; fall back to global python if absent.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m streamlit run app.py --server.port 8502 --browser.gatherUsageStats false
echo.
echo   The app has stopped.
pause
