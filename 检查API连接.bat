@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 scripts\运维工具.py check
) else (
  python scripts\运维工具.py check
)
pause
