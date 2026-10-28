@echo off
REM ============================================================================
REM  Lecture Recorder Server - one-time setup for Windows
REM
REM  Usage:   setup.cmd
REM
REM  What it does:
REM    1. Creates a Python virtual environment in .\.venv
REM    2. Installs Python dependencies from requirements.txt
REM    3. Checks for ffmpeg; offers to install it via winget if missing
REM
REM  After it finishes, run `python app.py` to start the server.
REM ============================================================================

setlocal

cd /d "%~dp0"

echo.
echo === Lecture Recorder Server - setup ===
echo.

REM --- 1. Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/
    echo Make sure to tick "Add python.exe to PATH" in the installer.
    exit /b 1
)
python --version

REM --- 2. Create / reuse venv ---
if not exist ".venv" (
    echo.
    echo Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: failed to create venv.
        exit /b 1
    )
) else (
    echo.
    echo Reusing existing venv in .venv
)

REM --- 3. Install dependencies ---
echo.
echo Installing Python dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    exit /b 1
)

REM --- 4. Check / install ffmpeg ---
echo.
echo Checking for ffmpeg ...
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg not found on PATH.
    where winget >nul 2>&1
    if not errorlevel 1 (
        echo Attempting install via winget (Gyan.FFmpeg) ...
        winget install --id Gyan.FFmpeg -e --source winget
        if errorlevel 1 (
            echo.
            echo WARNING: winget install failed.
            echo You can install ffmpeg manually from https://www.gyan.dev/ffmpeg/builds/
            echo or via Chocolatey: choco install ffmpeg
            echo WAV and MP3 uploads will still work without ffmpeg; M4A from
            echo iPhone Voice Memos needs it.
        ) else (
            echo ffmpeg installed. You may need to open a new terminal
            echo for PATH changes to take effect.
        )
    ) else (
        echo WARNING: neither ffmpeg nor winget is on PATH.
        echo Install ffmpeg manually from https://www.gyan.dev/ffmpeg/builds/
        echo or set LR_FFMPEG_PATH to the binary location.
    )
) else (
    ffmpeg -version
)

echo.
echo === Setup complete ===
echo.
echo To start the server:
echo     .venv\Scripts\python.exe app.py
echo Then open http://localhost:8000 in a browser.
echo.

endlocal
