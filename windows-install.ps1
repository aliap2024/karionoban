$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\Noban'
$log = Join-Path $env:TEMP 'Noban-install.log'
$offline = $env:NOBAN_OFFLINE -eq '1'
$assets = Join-Path (Split-Path -Parent $env:NOBAN_INSTALLER) 'offline-assets'
try {
    Start-Transcript -Path $log -Force | Out-Null
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows 64-bit is required.' }
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    $python = Join-Path $installDir 'Python\python.exe'
    if (-not (Test-Path $python)) {
        foreach ($key in @('HKCU:\Software\Python\PythonCore\3.12\InstallPath', 'HKLM:\Software\Python\PythonCore\3.12\InstallPath')) {
            if (Test-Path $key) {
                $candidate = Join-Path ((Get-Item $key).GetValue('')) 'python.exe'
                if (Test-Path $candidate) { $python = $candidate; break }
            }
        }
    }
    if (-not (Test-Path $python)) {
        if ($offline) {
            $download = Join-Path $assets 'python-3.12.10-amd64.exe'
            if (-not (Test-Path $download)) { throw 'Offline Python installer is missing. Extract the complete ZIP first.' }
        } else {
            Write-Host 'Downloading Python runtime. Please wait...'
            $download = Join-Path $env:TEMP ('Noban-Python-' + [Guid]::NewGuid() + '.exe')
            Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $download
        }
        $signature = Get-AuthenticodeSignature $download
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw 'Python download signature verification failed.'
        }
        Write-Host 'Installing Python runtime...'
        $target = Join-Path $installDir 'Python'
        $arguments = '/quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_pip=1 Include_tcltk=1 AssociateFiles=0 Shortcuts=0 PrependPath=0 TargetDir="' + $target + '"'
        $process = Start-Process -FilePath $download -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python installation failed: $($process.ExitCode)" }
        if (-not $offline) { Remove-Item -LiteralPath $download }
        if (-not (Test-Path $python)) { throw 'Python runtime was not installed in the expected directory.' }
    }
    Write-Host 'Preparing Noban and installing dependencies...'
    $venv = Join-Path $installDir 'Runtime'
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create application runtime.' }
    $appPython = Join-Path $venv 'Scripts\python.exe'
    $req = Join-Path $installDir 'requirements.txt'
    [IO.File]::WriteAllBytes((Join-Path $installDir 'app.py'), [Convert]::FromBase64String('__APP_BASE64__'))
    [IO.File]::WriteAllBytes($req, [Convert]::FromBase64String('__REQUIREMENTS_BASE64__'))
    if ($offline) {
        $wheels = Join-Path $assets 'wheels'
        if (-not (Test-Path $wheels)) { throw 'Offline packages are missing. Extract the complete ZIP first.' }
        & $appPython -m pip --isolated install --disable-pip-version-check --no-index --find-links $wheels -r $req
    } else {
        & $appPython -m pip install --disable-pip-version-check --index-url https://pypi.org/simple -r $req
    }
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See the installation log.' }
    & $appPython -c 'import tkinter, ezdxf, reportlab; from tkinter import Tk; root = Tk(); root.withdraw(); root.destroy()'
    if ($LASTEXITCODE -ne 0) { throw 'Application runtime check failed.' }
    $shell = New-Object -ComObject WScript.Shell
    $desktop = $shell.SpecialFolders.Item('Desktop')
    foreach ($folder in (@($desktop, [Environment]::GetFolderPath('Programs')) | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($folder)) { throw 'Windows did not return a shortcut folder.' }
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $shortcutPath = Join-Path $folder 'Noban.lnk'
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = Join-Path $venv 'Scripts\pythonw.exe'
        $shortcut.Arguments = '"' + (Join-Path $installDir 'app.py') + '"'
        $shortcut.WorkingDirectory = $installDir
        $shortcut.Description = 'Noban DXF to PDF'
        $shortcut.IconLocation = (Join-Path $venv 'Scripts\pythonw.exe') + ',0'
        $shortcut.Save()
        if (-not (Test-Path -LiteralPath $shortcutPath)) { throw "Shortcut could not be created: $shortcutPath" }
        Write-Host "Shortcut created: $shortcutPath"
    }
    Write-Host 'Installation complete. Use the Noban shortcut on your desktop.' -ForegroundColor Green
    Start-Process -FilePath (Join-Path $venv 'Scripts\pythonw.exe') -ArgumentList ('"' + (Join-Path $installDir 'app.py') + '"')
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host "Installation failed: $_" -ForegroundColor Red
    Write-Host "Log: $log"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
