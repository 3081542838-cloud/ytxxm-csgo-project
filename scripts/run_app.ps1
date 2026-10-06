param([string]$DataDir = (Join-Path $env:LOCALAPPDATA 'CS2POVHelper'))
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $taskRoot 'src'
& (Join-Path $taskRoot '.venv\Scripts\python.exe') -m cs2pov.app --data-dir $DataDir
exit $LASTEXITCODE
