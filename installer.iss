[Setup]
AppId=NobanDXFReport
AppName=Noban DXF Report
AppVersion=1.0.0
DefaultDirName={localappdata}\Programs\Noban
DefaultGroupName=Noban
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Noban-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Noban.exe

[Files]
Source: "dist\Noban.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Noban"; Filename: "{app}\Noban.exe"
Name: "{autodesktop}\Noban"; Filename: "{app}\Noban.exe"

[Run]
Filename: "{app}\Noban.exe"; Description: "Launch Noban"; Flags: nowait postinstall skipifsilent
