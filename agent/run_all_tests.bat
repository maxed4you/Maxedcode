@echo off
chcp 65001 >nul
cd /d "%~dp0"
python run_all_tests.py
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo All Maxedcode test suites passed.
) else (
  echo One or more Maxedcode test suites failed. See the output above.
)
pause
exit /b %RESULT%
