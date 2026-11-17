@echo off
REM ==========================================================================
REM  HMA Proxy Manager - chay tool tren Windows
REM  Nhap dup file nay. Lan dau se tu kiem tra / cai Python, OpenVPN, thu vien.
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title HMA Proxy Manager
cd /d "%~dp0"

REM ---------- 0. Tool da chay ngam (tu chay khi bat may)? Chi can mo giao dien ----------
powershell -NoProfile -Command "try{Invoke-WebRequest 'http://127.0.0.1:8800/api/status' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0}catch{ if($_.Exception.Response){exit 0}; exit 1}" >nul 2>&1
if not errorlevel 1 (
  echo Tool dang chay san. Dang mo giao dien http://127.0.0.1:8800 ...
  start "" "http://127.0.0.1:8800/"
  timeout /t 3 >nul
  exit /b 0
)

REM ---------- 1. Quyen Administrator (can de tao card mang ao + route VPN) ----------
net session >nul 2>&1
if errorlevel 1 (
  echo [*] Dang xin quyen Administrator... hay chon "Yes".
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
  exit /b
)

echo.
echo  ===============================================
echo     HMA Proxy Manager
echo  ===============================================
echo.

REM ---------- 2. Python 3.10+ ----------
set "PY="
if exist ".venv\Scripts\python.exe" goto :have_venv

where py >nul 2>&1 && (py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=py -3")
if not defined PY (
  where python >nul 2>&1 && (python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=python")
)
if not defined PY (
  echo [LOI] Chua co Python 3.10 tro len.
  where winget >nul 2>&1
  if not errorlevel 1 (
    choice /C YN /M "    Tu dong cai Python 3.12 bang winget"
    if !errorlevel! EQU 1 (
      winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
      echo.
      echo [*] Da cai xong Python. Hay DONG cua so nay va mo lai file start.bat.
      pause
      exit /b 0
    )
  )
  echo     Tai Python tai https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), roi chay lai file nay.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

echo [*] Tao moi truong Python rieng ^(.venv^)...
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo [LOI] Khong tao duoc .venv. Thu cai lai Python ^(chon "Add python.exe to PATH"^).
  pause
  exit /b 1
)

:have_venv
set "VPY=%~dp0.venv\Scripts\python.exe"

REM ---------- 3. Thu vien Python (chi cai lai khi requirements.txt thay doi) ----------
set "STAMP=.venv\requirements.installed"
set "NEED_PIP=1"
if exist "%STAMP%" (
  fc /b "requirements.txt" "%STAMP%" >nul 2>&1 && set "NEED_PIP=0"
)
if "%NEED_PIP%"=="1" (
  echo [*] Cai thu vien Python...
  "%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip >nul 2>&1
  "%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo [LOI] Cai thu vien that bai. Kiem tra ket noi Internet roi chay lai.
    pause
    exit /b 1
  )
  copy /y "requirements.txt" "%STAMP%" >nul
)

REM ---------- 4. OpenVPN Community (khong can o che do hma-app: dung ung dung HMA) ----------
set "OVPN="
if exist "config.yaml" findstr /R /C:"^provider: *hma-app" "config.yaml" >nul 2>&1 && goto :run
if exist "%ProgramFiles%\OpenVPN\bin\openvpn.exe" set "OVPN=%ProgramFiles%\OpenVPN\bin\openvpn.exe"
if not defined OVPN for %%I in (openvpn.exe) do if not "%%~$PATH:I"=="" set "OVPN=%%~$PATH:I"
if not defined OVPN (
  echo [LOI] Chua cai OpenVPN Community ^(can de chay cac ket noi VPN^).
  where winget >nul 2>&1
  if not errorlevel 1 (
    choice /C YN /M "    Tu dong cai OpenVPN bang winget"
    if !errorlevel! EQU 1 (
      winget install -e --id OpenVPNTechnologies.OpenVPN --accept-package-agreements --accept-source-agreements
      if exist "%ProgramFiles%\OpenVPN\bin\openvpn.exe" set "OVPN=%ProgramFiles%\OpenVPN\bin\openvpn.exe"
    )
  )
)
if not defined OVPN (
  echo     Tai OpenVPN tai https://openvpn.net/community-downloads/ ^(ban Windows 64-bit, cai mac dinh, giu TAP-Windows6^).
  echo     Tool van se mo; giao dien web se nhac lai buoc nay.
  start "" "https://openvpn.net/community-downloads/"
) else (
  echo [OK] OpenVPN: !OVPN!
)

:run
REM ---------- 5. Chay tool ----------
echo.
echo [*] Dang chay. Giao dien: http://127.0.0.1:8800  ^(trinh duyet se tu mo^)
echo [*] DONG CUA SO NAY = NGAT TAT CA VPN VA PROXY.  Nhan Ctrl+C de dung.
echo.
"%VPY%" -m hmaproxy web %*
set "CODE=%errorlevel%"
echo.
if not "%CODE%"=="0" echo [LOI] Tool da dung voi ma loi %CODE%. Xem thong bao phia tren.
pause
exit /b %CODE%
