@echo off
rem ---------------------------------------------------------------------------
rem  Prepare the private runtime of every module under modules\<id>\.
rem
rem  Same rules as start.bat: ASCII-only, CRLF, no parentheses blocks.
rem  The real work happens in scripts\bootstrap.py --modules-only.
rem
rem  Usage:
rem    scripts\setup-modules.bat                 only modules still missing one
rem    scripts\setup-modules.bat --only opsgen   just this module
rem    scripts\setup-modules.bat --force         rebuild even existing ones
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0.."

set "PY=%CD%\.venv\Scripts\python.exe"
if exist "%PY%" goto bootstrap

set "PY=python"
where python >nul 2>nul
if errorlevel 1 goto no_python

:bootstrap
"%PY%" "%CD%\scripts\bootstrap.py" --modules-only %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:no_python
echo.
echo   [X] Python was not found on this computer.
echo.
echo   Install Python 3.11 or newer from:
echo       https://www.python.org/downloads/
echo.
echo   During the installation, tick "Add python.exe to PATH".
echo.
pause
exit /b 1
