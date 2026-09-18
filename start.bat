@echo off
rem ---------------------------------------------------------------------------
rem  Personal Workspace - the only entry point.
rem
rem  Keep this file tiny, ASCII-only and CRLF-terminated.
rem  Every step and every localized message lives in scripts\bootstrap.py,
rem  because cmd.exe decodes .bat files with the console code page: non-ASCII
rem  text here gets mis-parsed, and the errors it prints point at lines that
rem  have nothing to do with the real problem.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto bootstrap

set "PY=python"
where python >nul 2>nul
if errorlevel 1 goto no_python

:bootstrap
"%PY%" "%~dp0scripts\bootstrap.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:no_python
echo.
echo   [X] Python was not found on this computer.
echo.
echo   Please install Python 3.11 or newer:
echo       https://www.python.org/downloads/
echo.
echo   During the installation, tick "Add python.exe to PATH".
echo   Then double-click this file again.
echo.
pause
exit /b 1
