# Start RealEyes (hidden background). Idempotent.
$ErrorActionPreference = 'Continue'
$dir = 'F:\scripts\realeyes'
# Python: override with $env:REALEYES_PYTHON, else 'python' from PATH.
$python = if ($env:REALEYES_PYTHON) { $env:REALEYES_PYTHON } else { 'python' }
$logDir = 'F:\realeyes\logs'
$stateDir = 'F:\realeyes\state'
$statusFile = Join-Path $stateDir 'status.json'
$pidFile = Join-Path $stateDir 'realeyes.pid'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

# Ensure the local LLM gateway is running (idempotent, quick health check).
try { & 'F:\scripts\llm\start_local_llm.ps1' | Out-Null } catch { }

# Already running? (pid alive and status fresh within 90s)
if ((Test-Path $statusFile) -and (Test-Path $pidFile)) {
    try {
        $st = Get-Content $statusFile -Raw | ConvertFrom-Json
        $running = Get-Process -Id ([int]$st.pid) -ErrorAction SilentlyContinue
        if ($running) {
            $age = [int]((Get-Date) - [datetime]::Parse($st.updated_at)).TotalSeconds
            if ($age -lt 90) { Write-Output "already running pid=$($st.pid)"; exit 0 }
        }
    } catch { }
}

$outLog = Join-Path $logDir 'realeyes_stdout.log'
$errLog = Join-Path $logDir 'realeyes_stderr.log'
$pyArgs = @('-u', (Join-Path $dir 'realeyes.py'), '--config', (Join-Path $dir 'config.yaml'))
Start-Process -FilePath $python -ArgumentList $pyArgs -WorkingDirectory $dir -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null

# Wait up to 20s for a fresh status file from the new process.
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    if (-not (Test-Path $statusFile)) { continue }
    try {
        $st = Get-Content $statusFile -Raw | ConvertFrom-Json
        $age = [int]((Get-Date) - [datetime]::Parse($st.updated_at)).TotalSeconds
        if ($age -le 5 -and (Get-Process -Id ([int]$st.pid) -ErrorAction SilentlyContinue)) {
            Write-Output "started pid=$($st.pid) camera_ok=$($st.camera_ok) screen_ok=$($st.screen_ok)"
            exit 0
        }
    } catch { }
}
Write-Output 'started (status file not refreshed yet; check F:\realeyes\logs\realeyes.log)'
