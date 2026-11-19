param(
  [string]$Title = "RealEyes",
  [string]$Message = ""
)
# 桌面通知（右下角气泡）。由 realeyes.py 以隐藏窗口方式调用。
try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $icon = New-Object System.Windows.Forms.NotifyIcon
  $icon.Icon = [System.Drawing.SystemIcons]::Information
  $icon.Visible = $true
  $icon.ShowBalloonTip(8000, $Title, $Message, [System.Windows.Forms.ToolTipIcon]::Info)
  Start-Sleep -Milliseconds 1500
  $icon.Visible = $false
  $icon.Dispose()
} catch {
  # 通知失败不影响主服务
  exit 0
}
exit 0
