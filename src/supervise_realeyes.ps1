# RealEyes supervisor: keeps the service alive (24h reliability).
# Started at login via HKCU Run; also safe to run manually.
# ASCII only: Windows PowerShell 5.1 cannot parse UTF-8 non-ASCII comments reliably.

$ErrorActionPreference = 'SilentlyContinue'
$dir = 'F:\scripts\realeyes'
$log = 'F:\realeyes\logs\supervisor.log'
$statusFile = 'F:\realeyes\state\status.json'

function Write-Log([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line -Encoding UTF8
}

Write-Log "supervisor starting; pid=$PID"
Start-Sleep -Seconds 20

while ($true) {
    $ok = $false
    if (Test-Path $statusFile) {
        try {
            $st = Get-Content $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $age = [int]((Get-Date) - [datetime]::Parse($st.updated_at)).TotalSeconds
            $proc = Get-Process -Id ([int]$st.pid) -ErrorAction SilentlyContinue
            if ($proc -and $age -le 120) { $ok = $true }
        } catch { }
    }
    if (-not $ok) {
        Write-Log "service down or status stale; restarting"
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir 'start_realeyes.ps1') | Out-Null
        } catch {
            Write-Log "start failed: $_"
        }
        Start-Sleep -Seconds 10
    }
    Start-Sleep -Seconds 20
}
