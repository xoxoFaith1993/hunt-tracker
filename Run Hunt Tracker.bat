@echo off
setlocal
cd /d "%~dp0"
set "PATH=%PATH%;%LOCALAPPDATA%\Python\bin;%LOCALAPPDATA%\Microsoft\WindowsApps"
where pyw >nul 2>nul
if %errorlevel%==0 (start "" pyw hunttracker.py & exit /b)
where pythonw >nul 2>nul
if %errorlevel%==0 (start "" pythonw hunttracker.py & exit /b)
py hunttracker.py
pause
