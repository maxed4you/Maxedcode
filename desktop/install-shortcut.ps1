# Shortcuts "maxedcode app" on the desktop and in the Start menu. Used by sync button 1 and by the installer
# (install.py): the signed app\maxedcode.exe when app\build-exe.ps1 can make one Windows runs, else app\maxedcode.ps1.
$app = Join-Path $PSScriptRoot 'app'
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $app 'build-exe.ps1')
$exe = Join-Path $app 'maxedcode.exe'
if ((Test-Path $exe) -and (Start-Process -FilePath $exe -ArgumentList '--shortcuts' -PassThru -Wait).ExitCode -eq 0) { 'Shortcuts: maxedcode.exe'; exit 0 }
& (Join-Path $app 'maxedcode.ps1') -Shortcuts
