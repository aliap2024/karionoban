Unicode True
!include "LogicLib.nsh"
Name "لیزر مرکزی"
OutFile "لیزر مرکزی.exe"
RequestExecutionLevel user
SetCompress off
ShowInstDetails show
AutoCloseWindow true
Page instfiles

Icon "assets\app.ico"
Section "Install"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File "offline-install.ps1"
    SetOutPath "$PLUGINSDIR\offline-assets"
    File /r "offline-assets/*"
    DetailPrint "در حال نصب نرم‌افزار لیزر مرکزی..."
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\offline-install.ps1"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_OK|MB_ICONSTOP "خطا در نصب برنامه."
        SetErrorLevel 1
        Abort
    ${EndIf}
    SetShellVarContext current
    CreateShortCut "$DESKTOP\لیزر مرکزی.lnk" "$LOCALAPPDATA\Programs\LaserMarkazi\Runtime\Scripts\pythonw.exe" '$\"$LOCALAPPDATA\Programs\LaserMarkazi\app.py$\"' "$LOCALAPPDATA\Programs\LaserMarkazi\app.ico" 0
    ClearErrors
SectionEnd
