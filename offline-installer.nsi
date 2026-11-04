Unicode True
!include "LogicLib.nsh"
Name "لیزر مرکزی"
Caption "نصب و به‌روزرسانی لیزر مرکزی 1.0.4"
OutFile "LaserMarkazi-Setup-1.0.4.exe"
VIProductVersion "1.0.4.0"
VIAddVersionKey "ProductName" "لیزر مرکزی"
VIAddVersionKey "FileDescription" "نصاب و به‌روزرسان لیزر مرکزی 1.0.4"
VIAddVersionKey "FileVersion" "1.0.4"
RequestExecutionLevel user
SetCompress off
ShowInstDetails show
AutoCloseWindow true
Page instfiles

Icon "assets\app.ico"
Section "Install"
    System::Call 'kernel32::SetEnvironmentVariableW(w "NOBAN_SETUP_EXE", w "$EXEPATH") i.r0'
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File "offline-install.ps1"
    SetOutPath "$PLUGINSDIR\offline-assets"
    File /r "offline-assets/*"
    DetailPrint "در حال نصب نرم‌افزار لیزر مرکزی..."
    StrCpy $0 "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
    IfFileExists "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" 0 +2
        StrCpy $0 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
    nsExec::ExecToLog '"$0" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\offline-install.ps1"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_OK|MB_ICONSTOP "خطا در نصب برنامه. گزارش خطا: $TEMP\LaserMarkazi-install.log"
        SetErrorLevel 1
        Abort
    ${EndIf}
SectionEnd
