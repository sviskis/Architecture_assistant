@echo off
rem ---------------------------------------------------------------------------
rem Architecture Assistant - operator panel launcher, console kept visible
rem
rem Same detection as START_ARCHITECTURE_ASSISTANT.bat (see that file for the
rem details): works from its own directory, sets PYTHONPATH to the project's
rem src tree, prefers "py -3.13", and only accepts a Python that is 3.11 or
rem newer, has tkinter and can import the operator panel.
rem
rem This version runs the panel in this window instead of hiding it, so its log
rem output is visible, and the window stays open after the panel closes: use it
rem when something needs looking at.
rem
rem Arguments are forwarded to the panel, e.g.
rem   START_ARCHITECTURE_ASSISTANT_CONSOLE.bat --database data\youtube_to_mp3.db
rem ---------------------------------------------------------------------------

setlocal
title Architecture Assistant - console launcher
cd /d "%~dp0" || goto :no_project
if not exist "%~dp0src\architecture_assistant_gui\__init__.py" goto :no_project
if not exist "%~dp0src\architecture_assistant\__init__.py" goto :no_project
set "PYTHONPATH=%~dp0src"

rem --- find an interpreter (same candidates and checks as the silent one) --
set "PYEXE="
set "PYNUM="
set "PYLAUNCH="
for %%c in ("py -3.13" "py -3" "python" "python3") do (
    if not defined PYEXE call :try %%c
)
if not defined PYEXE goto :no_python

echo Interpreter: "%PYEXE%"  (Python %PYNUM%)
echo Launcher:    %PYLAUNCH% -m architecture_assistant_gui %*
echo PYTHONPATH:  "%PYTHONPATH%"
echo Working dir: "%CD%"
echo.
echo The panel runs in this window. Closing the panel ends it; the log above
echo stays visible until you press a key.
echo.

"%PYEXE%" -m architecture_assistant_gui %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if "%EXITCODE%"=="0" echo Architecture Assistant closed normally.
if not "%EXITCODE%"=="0" echo Architecture Assistant failed to start or exited with code %EXITCODE%.
echo.
echo Press any key to close.
pause >nul
endlocal
exit /b %EXITCODE%

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
