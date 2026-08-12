@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 api_tools.py check
) else (
  python api_tools.py check
)
pause
