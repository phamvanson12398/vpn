@echo off
REM HMA Proxy Manager - chay giao dien web tren Windows
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :install
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m venv .venv) else (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
  echo Khong tim thay Python 3.10+. Cai tu https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
  pause
  exit /b 1
)

:install
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Cai thu vien that bai.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m hmaproxy web %*
pause
