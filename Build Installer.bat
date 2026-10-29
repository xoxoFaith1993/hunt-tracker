@echo off
setlocal
cd /d "%~dp0"
title Build Hunt Tracker installer
set "PATH=%PATH%;%LOCALAPPDATA%\Python\bin;%LOCALAPPDATA%\Microsoft\WindowsApps"
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
for /f %%v in ('py installer\version.py') do set "VER=%%v"
if not defined VER goto :fail

echo [1/3] Drawing the icon...
py installer\make_icon.py || goto :fail

echo [2/3] Building "Hunt Tracker.exe" v%VER%...
if exist installer\dist rmdir /s /q installer\dist
if exist installer\build rmdir /s /q installer\build
py -m PyInstaller --noconfirm --onefile --noconsole --name "Hunt Tracker" --icon "%~dp0installer\icon.ico" ^
   --add-data "%~dp0icons;icons" --add-data "%~dp0backdrop.png;." ^
   --distpath installer\dist --workpath installer\build --specpath installer hunttracker.py || goto :fail

echo [3/3] Building the installer...
"%ISCC%" /DAppVersion=%VER% installer\HuntTracker.iss || goto :fail

echo.
echo Done (v%VER%). Send this file to your brother:
echo   %~dp0installer\Output\HuntTrackerSetup.exe
if /i "%~1"=="quiet" exit /b 0
explorer "%~dp0installer\Output"
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
if /i "%~1"=="quiet" exit /b 1
pause
exit /b 1
