# Tạo Scheduled Task để HMA Proxy Manager tự chạy (ẩn, không cửa sổ) khi đăng nhập Windows,
# và tự khởi động lại nếu bị tắt. Chạy: chuột phải > "Run with PowerShell"
# Gỡ: Unregister-ScheduledTask -TaskName "HMA Proxy Manager" -Confirm:$false
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
    Write-Host "Chưa có .venv - hãy chạy start.bat một lần trước." -ForegroundColor Red
    exit 1
}
$action   = New-ScheduledTaskAction -Execute $pyw -Argument "-m hmaproxy web --no-browser" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Đợi Docker Desktop khởi động trước
$trigger.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "HMA Proxy Manager" -Action $action -Trigger $trigger -Settings $settings `
    -Description "HMA Proxy Manager web UI + giám sát" -Force | Out-Null
Write-Host "Đã cài tự khởi động. Giao diện: http://127.0.0.1:8800/" -ForegroundColor Green
Write-Host "Nhớ bật 'Start Docker Desktop when you sign in' trong Docker Desktop > Settings > General."
