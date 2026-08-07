@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 configure_api.py
) else (
  python configure_api.py
)
pause
