# Cài HMA Proxy Manager chạy như một "server": tự chạy NGAY KHI BẬT MÁY (trước cả khi đăng nhập),
# chạy ẩn bằng tài khoản SYSTEM (đủ quyền Administrator), tự chạy lại nếu bị tắt/crash.
# Dùng qua file "cai-tu-chay-khi-bat-may.bat". Gỡ bằng "go-tu-chay.bat".
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$TaskName = "HMA Proxy Manager"

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($Uninstall) { $arg += " -Uninstall" }
    Start-Process powershell -Verb RunAs -ArgumentList $arg
    exit
}

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "HMA Proxy Manager.url"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Da go tu chay khi bat may. Tat ca VPN/proxy da dung." -ForegroundColor Green
    } else {
        Write-Host "Chua cai tu chay, khong co gi de go."
    }
    Read-Host "Nhan Enter de dong"
    exit 0
}

$pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
    Write-Host "Chua co .venv - hay chay start.bat mot lan truoc (de cai Python/thu vien)." -ForegroundColor Red
    Read-Host "Nhan Enter de dong"
    exit 1
}
$config = Join-Path $dir "config.yaml"

# Neu dang chay bang start.bat thi nhac tat truoc (2 ban cung luc se tranh nhau cong 8800).
try {
    Invoke-WebRequest "http://127.0.0.1:8800/api/status" -UseBasicParsing -TimeoutSec 2 | Out-Null
    $running = $true
} catch { $running = [bool]$_.Exception.Response }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($running -and -not ($task -and $task.State -eq "Running")) {
    Write-Host "Tool dang chay trong cua so start.bat. Hay DONG cua so do roi chay lai file nay." -ForegroundColor Yellow
    Read-Host "Nhan Enter de dong"
    exit 1
}

$action    = New-ScheduledTaskAction -Execute $pyw -Argument "-m hmaproxy -c `"$config`" web --no-browser" -WorkingDirectory $dir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT20S"   # cho card mang khoi dong
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
             -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
             -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "HMA Proxy Manager: VPN + proxy + web UI (http://127.0.0.1:8800)" -Force | Out-Null

# Chay ngay, khong can khoi dong lai may
Start-ScheduledTask -TaskName $TaskName

# Loi tat tren Desktop de mo giao dien
"[InternetShortcut]`r`nURL=http://127.0.0.1:8800/`r`n" | Set-Content -Path $shortcut -Encoding ASCII

Write-Host ""
Write-Host "XONG! Tool da chay ngam va se TU CHAY moi khi bat may (khong can dang nhap)." -ForegroundColor Green
Write-Host "Giao dien: http://127.0.0.1:8800  (da tao loi tat 'HMA Proxy Manager' tren Desktop)"
Write-Host "Nhat ky:   $dir\.hmaproxy\logs\hmaproxy.log"
Write-Host "Go bo:     chay file go-tu-chay.bat"
Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:8800/"
Read-Host "Nhan Enter de dong"
