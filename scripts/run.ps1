# RealEyes console helper: run service in the foreground with a config (default src/config.yaml).
param(
  [string]$Config = "$PSScriptRoot\..\src\config.yaml",
  [int]$Duration = 0
)
# Python: override with $env:REALEYES_PYTHON, else 'python' from PATH.
$py = if ($env:REALEYES_PYTHON) { $env:REALEYES_PYTHON } else { 'python' }
$src = Join-Path $PSScriptRoot '..\src'
Set-Location $src
if ($Duration -gt 0) {
  & $py -u realeyes.py --config $Config --duration $Duration
} else {
  & $py -u realeyes.py --config $Config
}
exit $LASTEXITCODE
