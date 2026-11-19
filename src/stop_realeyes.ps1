# Stop RealEyes (only its own process, via status.json / realeyes.pid).
$ErrorActionPreference = 'Continue'
$stateDir = 'F:\realeyes\state'
$statusFile = Join-Path $stateDir 'status.json'
$pidFile = Join-Path $stateDir 'realeyes.pid'

$targetPid = $null
if (Test-Path $pidFile) {
    try { $targetPid = [int](Get-Content $pidFile -Raw) } catch { }
}
if (-not $targetPid -and (Test-Path $statusFile)) {
    try { $targetPid = (Get-Content $statusFile -Raw | ConvertFrom-Json).pid } catch { }
}
if (-not $targetPid) { Write-Output 'not running (no pid)'; exit 0 }
$p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if (-not $p) { Write-Output "pid $targetPid not found"; exit 0 }
Stop-Process -Id $targetPid -Force
Write-Output "stopped pid=$targetPid"
