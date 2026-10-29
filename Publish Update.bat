@echo off
setlocal
cd /d "%~dp0"
title Publish a Hunt Tracker update
set "PATH=%PATH%;%LOCALAPPDATA%\Python\bin;%LOCALAPPDATA%\Microsoft\WindowsApps"
rem Raises the version by one, builds the installer and uploads it to GitHub.
rem Every installed tracker picks it up the next time it opens.

set /p "NOTES=What changed (one line, Enter to skip): "
if not defined NOTES set "NOTES=Fixes and improvements."

for /f %%v in ('py installer\version.py --bump') do set "VER=%%v"
if not defined VER goto :fail
echo Publishing v%VER%...

call "%~dp0Build Installer.bat" quiet || goto :fail

gh release create "v%VER%" "installer\Output\HuntTrackerSetup.exe" --repo xoxoFaith1993/hunt-tracker ^
   --title "Hunt Tracker v%VER%" --notes "%NOTES%" || goto :fail

echo.
echo Published v%VER%. Installed trackers update the next time they open.
pause
exit /b 0

:fail
echo.
echo Publishing failed - see the messages above.
pause
exit /b 1
