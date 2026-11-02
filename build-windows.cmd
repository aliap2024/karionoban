@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail
.venv\Scripts\python -m PyInstaller --noconfirm --clean --windowed --onefile --name Noban app.py
if errorlevel 1 goto fail
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Portable app ready: dist\Noban.exe
  echo Install Inno Setup 6 and run this script again to create the installer.
  pause
  exit /b 0
)
"%ISCC%" installer.iss
if errorlevel 1 goto fail
echo Installer ready: dist\Noban-Setup.exe
pause
exit /b 0
:fail
echo Build failed. Install Python 3.12 or newer from python.org, including Tcl/Tk and the Python launcher.
pause
exit /b 1
