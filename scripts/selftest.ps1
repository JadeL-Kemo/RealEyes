# RealEyes selftest wrapper (no camera / no GPU required).
# Python: override with $env:REALEYES_PYTHON, else 'python' from PATH.
$py = if ($env:REALEYES_PYTHON) { $env:REALEYES_PYTHON } else { 'python' }
Set-Location (Join-Path $PSScriptRoot '..\src')
& $py -u realeyes.py --selftest
exit $LASTEXITCODE
