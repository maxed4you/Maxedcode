@echo off
rem maxedcode app: opens the window without a console window, like the shortcut: app\maxedcode.exe when setup built
rem it (app\build-exe.ps1), else app\maxedcode.ps1 (arguments, e.g. -Port 3091, go to the script).
if "%~1"=="" if exist "%~dp0app\maxedcode.exe" ( start "" "%~dp0app\maxedcode.exe" & exit /b 0 )
start "" "%SystemRoot%\System32\conhost.exe" --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0app\maxedcode.ps1" %*
