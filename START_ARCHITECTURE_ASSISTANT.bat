@echo off
rem ---------------------------------------------------------------------------
rem Architecture Assistant - operator panel launcher (double-click this file)
rem
rem What it does:
rem   1. works from its own directory (%~dp0), so the project can live anywhere
rem   2. puts the project's own src tree on PYTHONPATH
rem   3. finds a usable Python (prefers "py -3.13", then "py -3", "python",
rem      "python3"; a candidate is only used when it is Python 3.11 or newer,
rem      has tkinter and can import the operator panel)
rem   4. starts "python -m architecture_assistant_gui" WITHOUT a console window
rem      and closes this console immediately, so no stray window is left open
rem
rem Any argument you pass to this file is forwarded to the panel, e.g.
rem   START_ARCHITECTURE_ASSISTANT.bat --supervision
rem
rem If the panel cannot be started, the reason is printed here and this window
rem waits for a key press instead of vanishing. The panel's own startup output
rem (if it ever dies silently) is kept in
rem   %TEMP%\architecture_assistant_gui_launch.log
rem
rem START_ARCHITECTURE_ASSISTANT_CONSOLE.bat is the same launcher with the
rem console kept visible for logs and debugging.
rem ---------------------------------------------------------------------------

setlocal
title Architecture Assistant - launcher
cd /d "%~dp0" || goto :no_project
if not exist "%~dp0src\architecture_assistant_gui\__init__.py" goto :no_project
if not exist "%~dp0src\architecture_assistant\__init__.py" goto :no_project
set "PYTHONPATH=%~dp0src"

rem --- find an interpreter -----------------------------------------------
set "PYEXE="
set "PYNUM="
set "PYLAUNCH="
for %%c in ("py -3.13" "py -3" "python" "python3") do (
    if not defined PYEXE call :try %%c
)
if not defined PYEXE goto :no_python

rem --- start the panel without a console ---------------------------------
for %%i in ("%PYEXE%") do set "PYWDIR=%%~dpi"
set "PYWEXE=%PYWDIR%pythonw.exe"
if not exist "%PYWEXE%" goto :start_minimised

rem pythonw.exe runs the Tk panel with no console at all; the log file keeps any
rem output in case the panel dies right after starting.
set "LAUNCH_LOG=%TEMP%\architecture_assistant_gui_launch.log"
start "" "%PYWEXE%" -m architecture_assistant_gui %* 1>"%LAUNCH_LOG%" 2>&1
endlocal
exit /b 0

:start_minimised
rem This Python has no pythonw.exe: start it minimised instead.
start "Architecture Assistant" /min "%PYEXE%" -m architecture_assistant_gui %*
endlocal
exit /b 0

rem --- one candidate: keep it only if it passes every requirement ---------
rem Every probe inside "for /f" uses the *launcher* form ("py -3.13", "python")
rem instead of the quoted executable path: cmd's for/f cannot run a command that
rem starts with a quote, it silently produces nothing. The quoted %CANDEXE% is
rem used for ordinary commands, where quoting is required and safe.
:try
set "CAND=%~1"
%CAND% -c "import sys" >nul 2>&1
if errorlevel 1 goto :eof
set "CANDEXE="
set "CANDNUM="
for /f "delims=" %%e in ('%CAND% -c "import sys;print(sys.executable)" 2^>nul') do set "CANDEXE=%%e"
if not defined CANDEXE goto :eof
for /f "delims= " %%v in ('%CAND% -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2^>nul') do set "CANDNUM=%%v"
if not defined CANDNUM goto :eof
if %CANDNUM% LSS 311 goto :eof
"%CANDEXE%" -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :eof
"%CANDEXE%" -c "import architecture_assistant_gui" >nul 2>&1
if errorlevel 1 goto :eof
set "PYEXE=%CANDEXE%"
set "PYNUM=%CANDNUM%"
set "PYLAUNCH=%CAND%"
goto :eof

rem --- failures: say what is wrong and wait for a key --------------------
:no_project
echo Architecture Assistant failed to start.
echo This file must stay in the project root: the src tree was not found in
echo   "%~dp0src"
goto :fail

:no_python
echo Architecture Assistant failed to start.
echo No usable Python interpreter was found.
echo Looked for: py -3.13, py -3, python, python3.
echo A candidate must be Python 3.11 or newer, have tkinter, and be able to
echo import architecture_assistant_gui with PYTHONPATH="%PYTHONPATH%".
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo (keep the "tcl/tk" option) and try again.
goto :fail

:fail
echo.
echo Press any key to close.
pause >nul
endlocal
exit /b 1
