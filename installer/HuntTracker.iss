; Inno Setup script for Hunt Tracker. Build with "Build Installer.bat" in the folder above.
#define AppName "Hunt Tracker"
; Build Installer.bat passes the version from hunttracker.py (/DAppVersion=...).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "Hunt Tracker.exe"

[Setup]
AppId={{8F4E2A61-3C7B-4D9E-A1F5-6B2C8D0E4F17}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=xoxoFaith
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
; Per-user install: no admin prompt needed.
PrivilegesRequired=lowest
LicenseFile=LICENSE.txt
InfoAfterFile=README.txt
OutputDir=Output
OutputBaseFilename=HuntTrackerSetup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; Closes a running tracker before updating or uninstalling.
CloseApplications=yes

[Tasks]
Name: "autostart"; Description: "Open Hunt Tracker automatically whenever Hunt: Showdown starts"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userprograms}\{#AppName} - Read Me"; Filename: "{app}\README.txt"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
; A tiny helper that waits for Hunt to start, then opens the tracker.
Name: "{userstartup}\{#AppName} (opens with Hunt)"; Filename: "{app}\{#AppExe}"; Parameters: "--watch"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--watch"; Flags: nowait runhidden; Tasks: autostart
Filename: "{app}\{#AppExe}"; Description: "Open Hunt Tracker now"; Flags: nowait postinstall skipifsilent
; After a self-update (the tracker runs this installer with /UPDATE=1): reopen the tracker.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im ""{#AppExe}"""; Flags: runhidden; RunOnceId: "StopTracker"

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

// The tracker and its Hunt watcher are the same .exe: close both so it can be replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im "{#AppExe}"', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    // SuppressibleMsgBox: a silent uninstall skips the question and keeps the data.
    if SuppressibleMsgBox('Also delete your saved stats, players and screenshots?' + #13#10 +
              '(Choose No to keep them in case you reinstall.)',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(ExpandConstant('{localappdata}\HuntTracker'), True, True, True);
end;
