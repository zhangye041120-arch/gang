@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 api_tools.py configure
) else (
  python api_tools.py configure
)
pause
