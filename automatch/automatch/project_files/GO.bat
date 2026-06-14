@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title automatch setup

rem ===== Stage 0: make sure a real Python 3.10+ exists (the only thing that
rem ===== must happen before any .py can run). Everything else is bootstrap.py.

:python_check
where python >nul 2>nul
if errorlevel 1 goto maybe_install_python
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>nul
if errorlevel 1 goto maybe_install_python
goto have_python

:maybe_install_python
if exist ".automatch_py_tried" goto python_manual
echo placeholder> ".automatch_py_tried"
echo.
echo Installing Python 3.12 (one time)...
where winget >nul 2>nul
if errorlevel 1 goto curl_python
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
goto relaunch_fresh

:curl_python
echo winget not available; downloading Python from python.org...
curl.exe -L -o "%TEMP%\python-setup.exe" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
if not exist "%TEMP%\python-setup.exe" (
  echo.
  echo Could not download Python. Check your internet, then double-click "windows start here.bat" again.
  goto end
)
"%TEMP%\python-setup.exe" /passive PrependPath=1
goto relaunch_fresh

:python_manual
echo.
echo Python still isn't available. Install it yourself from
echo   https://www.python.org/downloads/   (tick "Add python.exe to PATH"),
echo then double-click "windows start here.bat" again.
goto end

:relaunch_fresh
echo.
echo Python is installed. Reopening in a fresh window so it's on your PATH...
start "" "%~f0"
exit /b

:have_python
if exist ".automatch_py_tried" del ".automatch_py_tried" >nul 2>nul

rem ===== Stage 1+: hand the heavy lifting to the Python doctor. It detects
rem ===== Docker/Ollama/models, installs/starts only what's missing, places
rem ===== files, then runs the wizard + first pipeline. Its exit code tells
rem ===== us whether to relaunch (PATH changed) or reboot (WSL2/Docker).

rem ===== Tell start.py to skip its own closing banner: the :ready landing
rem ===== below is the single "how to start" block for the double-click flow.
set AUTOMATCH_FROM_GOBAT=1
rem ===== Mark this as the INSTALLER. After setup is already complete, start.py
rem ===== honors this to inform-instead-of-scrape -- so re-opening this window
rem ===== never kicks off a job scrape the user didn't ask for. A direct
rem ===== `python start.py` (no flag) still scrapes as normal.
set AUTOMATCH_INSTALLER=1
python bootstrap.py
set RC=%errorlevel%

if "%RC%"=="10" goto relaunch_fresh2
if "%RC%"=="20" goto reboot
if "%RC%"=="0" goto ready
goto end

:relaunch_fresh2
echo.
echo Reopening in a fresh window to pick up what was just installed...
start "" "%~f0"
exit /b

:reboot
echo.
echo A restart is needed to finish setup (Docker / WSL2).
echo Your progress is saved -- after the restart, just double-click
echo "windows start here.bat" again and it picks up where it left off.
echo.
echo Restarting in 30 seconds. To CANCEL the restart, open a new terminal
echo and run:  shutdown /a   (then you can reboot yourself later).
shutdown /r /t 30
goto end

:ready
rem ===== Setup finished. Don't slam the window shut on a keypress -- drop the
rem ===== user into a normal prompt IN the project folder so they can actually
rem ===== run the thing (automatch) without hunting for it.
echo.
echo ==================================================================
echo  automatch is set up. This is now a normal terminal in your project folder.
echo.
rem ===== .env exists only for an ADVANCED (Discord bot) setup; basic installs
rem ===== have no `automatch` command, so point them at `python start.py`.
if exist "%~dp0.env" goto ready_adv
echo  Run  python start.py  in your terminal to scrape and score jobs, then
echo  open your matches.
goto ready_tail
:ready_adv
echo  Run  automatch  in your terminal to start the Discord bot.
echo.
echo  Then once the bot is running, DM the bot  !match  on Discord to start.
:ready_tail
echo.
echo  Type  exit  to close this window.
echo ==================================================================
echo.
cmd /k
exit /b

:end
echo.
pause
