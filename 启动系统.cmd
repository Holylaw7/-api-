@echo off
setlocal
cd /d "%~dp0"
py -3 --version >nul 2>nul
if errorlevel 1 goto try_python
py -3 launch.py
exit /b %errorlevel%
:try_python
python --version >nul 2>nul
if errorlevel 1 goto missing_python
python launch.py
exit /b %errorlevel%
:missing_python
echo Python 3.10 or later is required.
echo Install it from https://www.python.org/downloads/ and enable Add Python to PATH.
pause
exit /b 1
