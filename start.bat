@echo off
title Triage Desk
cd /d "%~dp0"
if not exist ".env" copy ".env.example" ".env" >nul
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.10 or newer and tick Add to PATH.
        pause
        exit /b 1
    )
)
echo Installing requirements...
venv\Scripts\python.exe -m pip install -q -r requirements.txt
if not exist "triage.db" venv\Scripts\python.exe -m triage_desk setup
where ollama >nul 2>&1
if errorlevel 1 (
    echo [NOTE] Ollama not found. Install it from https://ollama.com then run: ollama pull qwen2.5:7b
) else (
    ollama list | findstr /C:"qwen2.5" >nul || ollama pull qwen2.5:7b
)
echo.
echo Open http://127.0.0.1:8000  - demo clinician PINs 1234 or 5678, synthetic data only.
venv\Scripts\python.exe -m triage_desk web
pause
