@echo off
REM Cai HMA Proxy Manager tu chay khi bat may (chay ngam, quyen SYSTEM). Se hoi quyen Administrator.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Chay start.bat mot lan truoc de cai Python va thu vien, sau do chay lai file nay.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-autostart.ps1"
