@echo off
title SAC FP^&A Agent
cd /d "%~dp0"
echo.
echo   Starting SAC FP^&A Agent ...
echo   URL: http://localhost:8502
echo   (Keep this window open. Close it to stop the app.)
echo.
python -m streamlit run app.py --server.port 8502
echo.
echo   The app has stopped.
pause
