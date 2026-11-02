Unicode True
!include "LogicLib.nsh"
Name "Noban DXF Report"
OutFile "Noban-Offline-Setup.exe"
RequestExecutionLevel user
SetCompress off
ShowInstDetails show
AutoCloseWindow true
Page instfiles

Section "Install"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File "offline-install.ps1"
    SetOutPath "$PLUGINSDIR\offline-assets"
    File /r "offline-assets/*"
    DetailPrint "Installing Noban and all dependencies. No downloads are required. Please wait..."
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\offline-install.ps1"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_OK|MB_ICONSTOP "Installation failed. See %TEMP%\Noban-install.log for details."
        SetErrorLevel 1
        Abort
    ${EndIf}
    SetShellVarContext current
    ClearErrors
    CreateShortCut "$DESKTOP\Noban.lnk" "$LOCALAPPDATA\Programs\Noban\Runtime\Scripts\pythonw.exe" '$\"$LOCALAPPDATA\Programs\Noban\app.py$\"'
    IfErrors 0 +2
        MessageBox MB_OK|MB_ICONEXCLAMATION "Desktop shortcut could not be created. Open Noban from the Start menu."
SectionEnd
