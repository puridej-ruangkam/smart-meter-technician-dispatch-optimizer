@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  Offline install of everything this project needs on Windows.
rem  No internet: pip reads only the wheels\ folder next to this file.
rem
rem  Needs Python 3.14 (64-bit). If Python is missing and a
rem  python-3.14.*-amd64.exe installer was placed in this folder, it is
rem  run first.
rem
rem  Messages are English on purpose: Thai text in a .bat file can come
rem  out unreadable on a console with a different code page.
rem ---------------------------------------------------------------------

call :find_python
if defined PYCMD goto have_python

set "INSTALLER="
for %%F in ("python-3.14*-amd64.exe") do set "INSTALLER=%%~fF"
if not defined INSTALLER goto no_python

echo Python 3.14 was not found. Running the installer:
echo   %INSTALLER%
echo.
"%INSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_pip=1
echo.
echo Python has been installed. Close this window and double-click
echo install_windows.bat again to install the packages.
echo.
pause
exit /b 0

:no_python
echo.
echo Python 3.14 64-bit was not found on this computer.
echo.
echo Put the installer python-3.14.x-amd64.exe in this folder:
echo   %CD%
echo and double-click install_windows.bat again.
echo See README.md in this folder.
echo.
pause
exit /b 1

:have_python
echo Using Python: %PYCMD%
%PYCMD% --version
echo.

%PYCMD% -c "import tkinter" 2>nul
if errorlevel 1 (
  echo WARNING: tkinter is missing, so the planning screen will not open.
  echo Re-run the Python installer, choose Modify, and tick "tcl/tk and IDLE".
  echo.
)

echo Installing packages from the wheels folder (no internet)...
echo.
%PYCMD% -m pip install --user --no-index --find-links "%~dp0wheels" -r "%~dp0requirements-lock.txt"
if errorlevel 1 goto failed

echo.
echo Checking the installation...
%PYCMD% -c "import pandas, numpy, openpyxl, ortools, scipy, pytest, win32com.client; print('All packages OK')"
if errorlevel 1 goto failed

echo.
echo ----------------------------------------------------------------
echo Done. Start the program with planner.bat in the project folder.
echo ----------------------------------------------------------------
echo.
pause
exit /b 0

:failed
echo.
echo ----------------------------------------------------------------
echo The installation did not finish. The reason is printed above.
echo Take a photo of this window and send it to the project owner.
echo ----------------------------------------------------------------
echo.
pause
exit /b 1

rem ---------------------------------------------------------------------
:find_python
rem  Only a 64-bit Python 3.14 will do: the wheels are built for it.
set "PYCMD="
set "CHECK=import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 14) and struct.calcsize('P') == 8 else 1)"
where py >nul 2>nul && (
  py -3.14 -c "%CHECK%" >nul 2>nul && set "PYCMD=py -3.14"
)
if not defined PYCMD (
  where python >nul 2>nul && (
    python -c "%CHECK%" >nul 2>nul && set "PYCMD=python"
  )
)
if not defined PYCMD (
  if exist "C:\Python314\python.exe" (
    "C:\Python314\python.exe" -c "%CHECK%" >nul 2>nul && set "PYCMD=C:\Python314\python.exe"
  )
)
if not defined PYCMD (
  if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" -c "%CHECK%" >nul 2>nul && set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  )
)
exit /b 0
