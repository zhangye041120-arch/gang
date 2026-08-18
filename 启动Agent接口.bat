@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动小辽智能体 API，端口统一读取 .env 的 API_PORT。
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 启动API.py
) else (
  python 启动API.py
)
pause
