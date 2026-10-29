@echo off
setlocal
cd /d "%~dp0"
title Build Hunt Tracker.exe
set "PATH=%PATH%;%LOCALAPPDATA%\Python\bin;%LOCALAPPDATA%\Microsoft\WindowsApps;%LOCALAPPDATA%\Programs\Python\Launcher"
echo Building a standalone Hunt Tracker.exe (needs Python on THIS PC only, one time)...
py -m pip install --user --upgrade pyinstaller
if errorlevel 1 (echo pip failed & pause & exit /b 1)
py -m PyInstaller --onefile --noconsole --name "Hunt Tracker" hunttracker.py
if errorlevel 1 (echo build failed & pause & exit /b 1)
echo.
echo Done. Send this file to your brother:  %~dp0dist\Hunt Tracker.exe
explorer "%~dp0dist"
pause
