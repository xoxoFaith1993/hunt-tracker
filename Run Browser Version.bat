@echo off
setlocal
cd /d "%~dp0"
set "PATH=%PATH%;%LOCALAPPDATA%\Python\bin;%LOCALAPPDATA%\Microsoft\WindowsApps"
where py >nul 2>nul
if %errorlevel%==0 (py hunttracker.py --web) else (python hunttracker.py --web)
pause
