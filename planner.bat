@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  Find a real Python.
rem
rem  Windows ships a fake "python.exe" that only opens the Microsoft Store.
rem  Running it does nothing visible and the window closes, which looks
rem  exactly like "nothing happened". The py launcher, installed with real
rem  Python from python.org, is checked first for that reason.
rem
rem  Messages here are English on purpose: a .bat file with Thai text can
rem  come out as rubbish on a machine with a different console code page,
rem  and an unreadable error message is worse than no error message.
rem ---------------------------------------------------------------------

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>nul && set "PYCMD=python"
)
rem  Last resort: a machine where Python was installed but never added to
rem  PATH.
if not defined PYCMD (
  if exist "C:\Python314\python.exe" set "PYCMD=C:\Python314\python.exe"
)

if not defined PYCMD (
  echo.
  echo Python was not found on this computer.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo During installation, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

if not exist "planner.py" (
  echo.
  echo planner.py is not in this folder:
  echo   %CD%
  echo.
  echo If this folder is in OneDrive, the file may not be downloaded yet.
  echo Right-click the project folder and choose "Always keep on this device".
  echo.
  pause
  exit /b 1
)

echo Starting the planner with: %PYCMD%
echo.
%PYCMD% planner.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo ----------------------------------------------------------------
  echo The planner stopped with error code %RC%.
  echo The reason is printed above this line.
  echo.
  echo Most common causes:
  echo   - a package is missing. Install them with:
  echo       %PYCMD% -m pip install -r requirements.txt
  echo   - tkinter is missing. Re-run the Python installer and make sure
  echo     "tcl/tk and IDLE" is ticked.
  echo ----------------------------------------------------------------
  echo.
  pause
)
