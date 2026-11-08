@echo off
chcp 65001 >nul
where wt >nul 2>nul
if %errorlevel%==0 (
  start "" wt -w new --title "maxedcode" -d "%CD%" python "%~dp0local_agent.py"
) else (
  start "maxedcode" cmd /k python "%~dp0local_agent.py"
)