"""Build the single-file Windows online installer without a Windows build host."""
import base64
from pathlib import Path
import sys


def build(offline=False):
    root = Path(__file__).resolve().parent
    script = (root / 'windows-install.ps1').read_text()
    placeholders = [
        ('__APP_BASE64__', root / 'app.py'),
        ('__REQUIREMENTS_BASE64__', root / 'requirements.txt'),
        ('__APP_ICO_BASE64__', root / 'assets' / 'app.ico'),
        ('__APP_PNG_BASE64__', root / 'assets' / 'app.png'),
    ]
    for placeholder, path in placeholders:
        encoded = base64.b64encode(path.read_bytes()).decode('ascii')
        assert placeholder in script, f'Missing installer placeholder: {placeholder}'
        script = script.replace(placeholder, encoded)
        assert base64.b64decode(encoded) == path.read_bytes()
    for placeholder, _ in placeholders:
        assert placeholder not in script
    header = '''@echo off
setlocal
set "NOBAN_INSTALLER=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$content = [IO.File]::ReadAllText($env:NOBAN_INSTALLER); $script = ($content -split '(?m)^:NOBAN_PAYLOAD\\r?\\n', 2)[1]; & ([ScriptBlock]::Create($script))"
set "NOBAN_RESULT=%ERRORLEVEL%"
pause
exit /b %NOBAN_RESULT%
:NOBAN_PAYLOAD
'''
    if offline:
        header = header.replace('setlocal\n', 'setlocal\nset "NOBAN_OFFLINE=1"\n')
    else:
        header = header.replace('setlocal\n', 'setlocal\nset "NOBAN_OFFLINE=0"\n')
    output = root / ('Install-Noban-Offline.cmd' if offline else 'Install-Noban.cmd')
    output.write_bytes((header + script).replace('\r\n', '\n').replace('\n', '\r\n').encode('ascii'))
    assert output.read_bytes().count(b'\r\n:NOBAN_PAYLOAD\r\n') == 1
    if offline:
        prefix = "$env:NOBAN_OFFLINE = '1'\n$env:NOBAN_INSTALLER = $PSCommandPath\n"
        (root / 'offline-install.ps1').write_bytes((prefix + script).replace('\r\n', '\n').replace('\n', '\r\n').encode('ascii'))
    print(output)


if __name__ == '__main__':
    build(offline='--offline' in sys.argv)
