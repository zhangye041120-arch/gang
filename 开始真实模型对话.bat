@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 运行\run_agent.py
) else (
  python 运行\run_agent.py
)
pause
