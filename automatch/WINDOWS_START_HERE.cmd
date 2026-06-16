@echo off
rem WINDOWS_START_HERE: installs Python via winget if missing, then hands off
rem to start.py (the setup wizard). After a Python install you reopen this file
rem in a FRESH terminal -- that's the only way Windows exposes the new Python
rem on PATH. Setup is resumable, so re-running this never loses progress.
cd /d "%~dp0project_files"

rem the Store alias stub fails this; real Python 3.10+ passes
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if %errorlevel%==0 (
    python start.py
    echo.
    echo ============================================================
    echo  If setup said it needs a reboot or more steps, that's fine:
    echo  nothing is lost. Just double-click WINDOWS_START_HERE.cmd
    echo  again to pick up where it left off. This window stays open
    echo  so you can read the messages above.
    echo ============================================================
    pause
    exit /b
)

where winget >nul 2>&1
if errorlevel 1 (
    echo winget is not available; install Python 3.12 from https://python.org,
    echo then double-click WINDOWS_START_HERE.cmd again.
    pause
    exit /b 1
)

echo installing Python 3.12 via winget...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo winget install failed; install Python from https://python.org, then
    echo double-click WINDOWS_START_HERE.cmd again.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Python installed successfully.
echo.
echo  CLOSE THIS WINDOW, then double-click WINDOWS_START_HERE.cmd
echo  again to continue. Windows only adds Python to NEW terminals,
echo  so this one can't see it yet. Nothing is lost.
echo ============================================================
pause
exit /b
