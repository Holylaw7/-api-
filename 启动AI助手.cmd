@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 --version >nul 2>nul
if not errorlevel 1 goto use_py
python --version >nul 2>nul
if not errorlevel 1 goto use_python
echo 未找到 Python。请安装 Python 3.10 或更新版本，并勾选 Add Python to PATH。
echo 安装完成后重新双击本文件。下载地址：https://www.python.org/downloads/
pause
exit /b 1
:use_py
py -3 launch_ai.py
exit /b %errorlevel%
:use_python
python launch_ai.py
exit /b %errorlevel%
