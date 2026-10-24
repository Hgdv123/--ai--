@echo off
chcp 65001 >nul 2>nul
title WenMai Launcher

rem ---- Resolve this script's own folder. ----
rem Use %~dp0 (the script's own directory) as the single source of path truth.
rem Do NOT build paths from %CD%: with Chinese folder names, `cd /d` has been
rem observed to land on a parent directory, so "%CD%\_launch.py" then misses.
rem This file must stay PURE ASCII -- cmd.exe reads a .bat with the system
rem code page (GBK on Chinese Windows) even after chcp 65001, so non-ASCII
rem bytes anywhere (comments, paths, echoed text) can become stray commands.
rem All Chinese messages live in _launch.py, which prints them as UTF-8.
setlocal
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

echo.
echo ============================================================
echo    WenMai  ^|  Literature Review Writer + Citation Checker
echo ============================================================
echo.

echo Folder : %HERE%
echo.

rem ---- 1. locate Python ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  for %%V in (313 312 311 310 39) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  )
)
if not defined PY (
  for %%V in (313 312 311 310 39) do (
    if not defined PY if exist "C:\Program Files\Python%%V\python.exe" set "PY=C:\Program Files\Python%%V\python.exe"
  )
)

if not defined PY (
  echo   ------------------------------------------------------------
  echo    Python 3.9 or newer is required, but was not found.
  echo.
  echo    How to fix:
  echo      1. Download from https://www.python.org/downloads/
  echo      2. During setup, TICK "Add python.exe to PATH"
  echo      3. Then run this file again
  echo.
  echo    Detailed steps: see the installation guide, section 3.
  echo   ------------------------------------------------------------
  echo.
  pause
  exit /b 1
)
echo Python : %PY%
echo.

rem ---- 2. locate the launcher ----
rem Look in this folder first, then one level down (the code folder may keep
rem the package in a subdirectory).
set "WM_LAUNCH="
if exist "%HERE%\_launch.py" set "WM_LAUNCH=%HERE%\_launch.py"
if not defined WM_LAUNCH (
  for /d %%D in ("%HERE%\*") do (
    if not defined WM_LAUNCH if exist "%%~fD\_launch.py" set "WM_LAUNCH=%%~fD\_launch.py"
  )
)

if not defined WM_LAUNCH (
  echo   ------------------------------------------------------------
  echo    Launcher not found: _launch.py
  echo    The folder structure looks incomplete.
  echo    Please extract the WHOLE archive again, then run this file
  echo    from inside the extracted folder.
  echo    See the installation guide, section 6, question Q3.
  echo   ------------------------------------------------------------
  echo.
  pause
  exit /b 1
)

echo.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%PY%" -X utf8 "%WM_LAUNCH%" %*

if errorlevel 1 (
  echo.
  echo   ------------------------------------------------------------
  echo    Startup failed. See the messages above,
  echo    or the installation guide, section 6, FAQ.
  echo   ------------------------------------------------------------
  echo.
  pause
  exit /b 1
)

echo.
echo ------------------------------------------------------------
echo  Stopped. Press any key to close this window.
echo ------------------------------------------------------------
pause >nul
