# Tạo Scheduled Task để HMA Proxy Manager tự chạy ẩn (quyền Administrator) khi đăng nhập Windows,
# và tự chạy lại nếu bị tắt. Cách dùng: chuột phải file này > "Run with PowerShell" (sẽ hỏi quyền Admin).
# Gỡ: Unregister-ScheduledTask -TaskName "HMA Proxy Manager" -Confirm:$false
$ErrorActionPreference = "Stop"
$principalCheck = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
    Write-Host "Chưa có .venv - hãy chạy start.bat một lần trước." -ForegroundColor Red
    Read-Host "Nhấn Enter để thoát"
    exit 1
}
$action    = New-ScheduledTaskAction -Execute $pyw -Argument "-m hmaproxy web --no-browser" -WorkingDirectory $dir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"   # đợi mạng sẵn sàng
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "HMA Proxy Manager" -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "HMA Proxy Manager: VPN + proxy + web UI" -Force | Out-Null
Write-Host "Đã cài tự khởi động. Giao diện: http://127.0.0.1:8800/" -ForegroundColor Green
Write-Host "Chạy ngay bây giờ: Start-ScheduledTask -TaskName 'HMA Proxy Manager'"
Read-Host "Nhấn Enter để đóng"
