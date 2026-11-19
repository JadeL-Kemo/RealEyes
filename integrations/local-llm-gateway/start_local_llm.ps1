# Start the on-demand local LLM gateway (llama.cpp workers behind :8322).
# Safe to call repeatedly: exits if the gateway is already answering.
$ErrorActionPreference = 'Stop'
$py = if ($env:REALEYES_PYTHON) { $env:REALEYES_PYTHON } else { 'python' }
$gateway = 'F:\scripts\llm\local_llm_gateway.py'
$logDir = 'F:\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8322/healthz' -TimeoutSec 3
    if ($r.ok) { exit 0 }
} catch { }

$stdout = Join-Path $logDir 'local_llm_gateway_stdout.log'
$stderr = Join-Path $logDir 'local_llm_gateway_stderr.log'
$args = @('-u', $gateway)
Start-Process -FilePath $py -ArgumentList $args -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
Start-Sleep -Seconds 2
try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8322/healthz' -TimeoutSec 10
    if (-not $r.ok) { throw 'gateway not healthy' }
} catch {
    Write-Error "gateway failed to start: $($_.Exception.Message); see $stderr"
    exit 1
}
