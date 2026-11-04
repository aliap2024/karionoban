[Setup]
AppId=LaserMarkazi
AppName=لیزر مرکزی
AppVersion=1.0.4
DefaultDirName={localappdata}\Programs\لیزر مرکزی
DefaultGroupName=لیزر مرکزی
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=لیزر مرکزی-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Noban.exe

[Files]
Source: "dist\Noban.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\لیزر مرکزی"; Filename: "{app}\Noban.exe"; IconFilename: "{app}\app.ico"
Name: "{autodesktop}\لیزر مرکزی"; Filename: "{app}\Noban.exe"; IconFilename: "{app}\app.ico"

[Run]
Filename: "{app}\Noban.exe"; Description: "Launch لیزر مرکزی"; Flags: nowait postinstall skipifsilent
