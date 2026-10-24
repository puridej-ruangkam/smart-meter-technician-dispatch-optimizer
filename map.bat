@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem  Opens the route map through http://localhost instead of opening the file
rem  directly. OpenStreetMap refuses tile requests that carry no Referer
rem  header, and a file opened by double-click sends none.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
  if exist "C:\Python314\python.exe" set "PYCMD=C:\Python314\python.exe"
)

if not defined PYCMD (
  echo.
  echo Python was not found on this computer.
  echo Install it from https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PYCMD% tools\serve_map.py %*
if errorlevel 1 (
  echo.
  pause
)
