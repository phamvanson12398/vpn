@echo off
REM Go che do tu chay khi bat may va dung tool dang chay ngam. Se hoi quyen Administrator.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-autostart.ps1" -Uninstall
