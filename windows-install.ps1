$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$shortcutTitle = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('2YTbjNiy2LEg2YXYsdqp2LLbjA=='))
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\LaserMarkazi'
$log = Join-Path $env:TEMP 'LaserMarkazi-install.log'
$offline = $env:NOBAN_OFFLINE -eq '1'
$assets = Join-Path (Split-Path -Parent $env:NOBAN_INSTALLER) 'offline-assets'
$legacyDirs = @($installDir, (Join-Path $env:LOCALAPPDATA 'Programs\Noban'), (Join-Path $env:LOCALAPPDATA ('Programs\' + $shortcutTitle)))

function Test-AppShortcut($shortcut) {
    $target = [Environment]::ExpandEnvironmentVariables($shortcut.TargetPath)
    $arguments = [Environment]::ExpandEnvironmentVariables($shortcut.Arguments)
    foreach ($directory in $legacyDirs) {
        if ($target -ieq (Join-Path $directory 'Noban.exe')) { return $true }
        $app = Join-Path $directory 'app.py'
        if (($arguments -ieq $app -or $arguments -ieq ('"' + $app + '"')) -and
            [IO.Path]::GetFileName($target) -in @('python.exe', 'pythonw.exe')) { return $true }
    }
    return $false
}

function Test-AppInstaller($file) {
    if ($file.Extension -notin @('.exe', '.cmd')) { return $false }
    if ($file.Extension -ieq '.exe') {
        $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($file.FullName)
        if ($version.ProductName -in @($shortcutTitle, 'Noban')) { return $true }
    }
    $name = $file.BaseName -replace '\s*\(\d+\)$', ''
    if ($name -notin @($shortcutTitle, 'Noban-Offline-Setup', 'Noban-Setup', 'Install-Noban', 'Install-Noban-Offline') -and
        $name -notmatch '^LaserMarkazi-Setup-\d+(\.\d+)*$') { return $false }
    # Old NSIS installers have no version metadata; identify their embedded payload.
    $content = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($file.FullName))
    return $content.Contains('2YTbjNiy2LEg2YXYsdqp2LLbjA==') -and
        $content.Contains('python-3.12.10-amd64.exe') -and
        ($content.Contains('Programs\LaserMarkazi') -or $content.Contains('Programs\Noban'))
}

function Remove-OldAppFiles($folders, $shell, $removeInstallers) {
    $errors = @()
    foreach ($folder in ($folders | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $folder)) { continue }
        foreach ($file in (Get-ChildItem -LiteralPath $folder -File -Force)) {
            if ($file.FullName -ieq $env:NOBAN_SETUP_EXE -or $file.FullName -ieq $env:NOBAN_INSTALLER) { continue }
            try {
                $owned = $false
                if ($file.Extension -ieq '.lnk') {
                    if ($file.BaseName -in @($shortcutTitle, 'Noban')) {
                        $owned = $true
                    } else {
                        try {
                            $owned = Test-AppShortcut ($shell.CreateShortcut($file.FullName))
                        } catch {
                            $owned = $false
                        }
                    }
                } elseif ($removeInstallers) {
                    $owned = Test-AppInstaller $file
                }
                if ($owned) {
                    Remove-Item -LiteralPath $file.FullName -Force
                    Write-Host "Removed old application file: $($file.FullName)"
                }
            } catch {
                $errors += "$($file.FullName): $_"
            }
        }
    }
    if ($errors.Count) { throw ('Cannot remove old application files: ' + ($errors -join '; ')) }
}

function Test-BasePython($candidate) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $false }
    try {
        & $candidate -I -c 'import sys, tkinter, venv, pip; assert sys.version_info[:2] == (3, 12); assert sys.maxsize > 2**32; assert tuple(int(x) for x in pip.__version__.split(chr(46))[:2]) >= (22, 3)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-BasePython {
    $candidates = @((Join-Path $installDir 'Python\python.exe'))
    foreach ($name in @('HKCU:\Software\Python\PythonCore\3.12\InstallPath', 'HKLM:\Software\Python\PythonCore\3.12\InstallPath')) {
        $key = Get-Item -LiteralPath $name -ErrorAction SilentlyContinue
        if ($key) {
            try {
                $executable = $key.GetValue('ExecutablePath')
                if ($executable) { $candidates += $executable }
                $directory = $key.GetValue('')
                if ($directory) { $candidates += Join-Path $directory 'python.exe' }
            } finally { $key.Close() }
        }
    }
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles 'Python312\python.exe' }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-BasePython $candidate) { return $candidate }
    }
    return $null
}

try {
    Start-Transcript -Path $log -Force | Out-Null
    Write-Host 'Laser Markazi installer / updater 1.0.4'
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows 64-bit is required.' }
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    $running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'Noban.exe'" |
        Where-Object {
            $ownedProcess = $false
            foreach ($directory in $legacyDirs) {
                if (($_.CommandLine -and $_.CommandLine.IndexOf((Join-Path $directory 'app.py'), [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
                    $_.ExecutablePath -ieq (Join-Path $directory 'Noban.exe')) { $ownedProcess = $true }
            }
            $ownedProcess
        }
    if ($running) { throw 'Close Laser Markazi before updating, then run this installer again.' }
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    if ([string]::IsNullOrWhiteSpace($desktop)) { $desktop = $shell.SpecialFolders.Item('Desktop') }
    $programs = [Environment]::GetFolderPath('Programs')
    if ([string]::IsNullOrWhiteSpace($desktop) -or [string]::IsNullOrWhiteSpace($programs)) {
        throw 'Windows Desktop or Start Menu folder could not be resolved.'
    }
    # Scan old locations, but create shortcuts only in the two Windows known folders.
    $desktopFolders = @($desktop, (Join-Path $env:USERPROFILE 'Desktop'))
    foreach ($oneDrive in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
        if ($oneDrive) { $desktopFolders += Join-Path $oneDrive 'Desktop' }
    }
    $publicDesktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
    $commonPrograms = [Environment]::GetFolderPath('CommonPrograms')
    $desktopFolders += $publicDesktop
    $shortcutFolders = @($programs, $commonPrograms)
    foreach ($folder in @($programs, $commonPrograms)) {
        if ($folder) {
            $shortcutFolders += @((Join-Path $folder 'Noban'), (Join-Path $folder $shortcutTitle))
        }
    }
    Remove-OldAppFiles $desktopFolders $shell $true
    Remove-OldAppFiles $shortcutFolders $shell $false
    $python = Find-BasePython
    if (-not $python) {
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
        $arguments = '/quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_pip=1 Include_tcltk=1 AssociateFiles=0 Shortcuts=0 PrependPath=0 TargetDir="' + $target + '" /log "' + (Join-Path $env:TEMP 'LaserMarkazi-python-install.log') + '"'
        $process = Start-Process -FilePath $download -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python installation failed: $($process.ExitCode)" }
        if (-not $offline) { Remove-Item -LiteralPath $download }
        $python = Find-BasePython
        if (-not $python) { throw 'No working Python 3.12 x64 with Tk and pip was found after installation. See the Python installer log.' }
    }
    Write-Host "Preparing $shortcutTitle and installing dependencies..."
    Write-Host "Using Python: $python"
    $runtime = Join-Path $installDir 'Runtime'
    # Skip ensurepip; base pip installs directly into the app's private runtime.
    & $python -I -m venv --without-pip $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the private runtime (without pip). See the installation log.' }
    $appPython = Join-Path $runtime 'Scripts\python.exe'
    $appPythonw = Join-Path $runtime 'Scripts\pythonw.exe'
    $req = Join-Path $installDir 'requirements.txt'
    [IO.File]::WriteAllBytes($req, [Convert]::FromBase64String('__REQUIREMENTS_BASE64__'))
    [IO.File]::WriteAllBytes((Join-Path $installDir 'app.ico'), [Convert]::FromBase64String('__APP_ICO_BASE64__'))
    [IO.File]::WriteAllBytes((Join-Path $installDir 'app.png'), [Convert]::FromBase64String('__APP_PNG_BASE64__'))
    Copy-Item -LiteralPath (Join-Path $installDir 'app.ico') -Destination (Join-Path $installDir 'app-1.0.4.ico') -Force
    if ($offline) {
        $wheels = Join-Path $assets 'wheels'
        if (-not (Test-Path $wheels)) { throw 'Offline packages are missing. Extract the complete ZIP first.' }
        & $python -I -m pip --isolated --python $appPython install --disable-pip-version-check --no-index --find-links $wheels -r $req
    } else {
        & $python -I -m pip --isolated --python $appPython install --disable-pip-version-check --index-url https://pypi.org/simple -r $req
    }
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See the installation log.' }
    if (-not (Test-Path $appPythonw)) { throw 'Python GUI runtime is missing. Reinstall the private Python runtime.' }
    & $appPython -I -c 'import tkinter, ezdxf, reportlab; from OCP.STEPControl import STEPControl_Reader; from tkinter import Tk; root = Tk(); root.withdraw(); root.destroy()'
    if ($LASTEXITCODE -ne 0) { throw 'Application runtime check failed.' }
    [IO.File]::WriteAllBytes((Join-Path $installDir 'app.py'), [Convert]::FromBase64String('__APP_BASE64__'))
    foreach ($folder in @($desktop, $programs)) {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $shortcutPath = Join-Path $folder ($shortcutTitle + '.lnk')
        if (Test-Path -LiteralPath $shortcutPath) {
            Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
        }
        $tempShortcutPath = Join-Path $env:TEMP ([Guid]::NewGuid().ToString('N') + '.lnk')
        try {
            $shortcut = $shell.CreateShortcut($tempShortcutPath)
            $shortcut.TargetPath = $appPythonw
            $shortcut.Arguments = '"' + (Join-Path $installDir 'app.py') + '"'
            $shortcut.WorkingDirectory = $installDir
            $shortcut.Description = 'Laser Markazi - DXF to PDF'
            $shortcut.IconLocation = (Join-Path $installDir 'app-1.0.4.ico') + ',0'
            $shortcut.Save()
            Move-Item -LiteralPath $tempShortcutPath -Destination $shortcutPath -Force
            Write-Host "Shortcut updated: $shortcutPath"
        } finally {
            if (Test-Path -LiteralPath $tempShortcutPath) {
                Remove-Item -LiteralPath $tempShortcutPath -Force -ErrorAction SilentlyContinue
            }
        }
    }
    Write-Host "Installation complete. Use the $shortcutTitle shortcut on your desktop." -ForegroundColor Green
    Start-Process -FilePath $appPythonw -ArgumentList ('"' + (Join-Path $installDir 'app.py') + '"')
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host "Installation failed: $_" -ForegroundColor Red
    Write-Host "Log: $log"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
