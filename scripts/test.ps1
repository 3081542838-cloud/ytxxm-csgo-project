param([string[]]$TestPaths = @('tests'))
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project virtual environment is missing.' }
Push-Location -LiteralPath $taskRoot
try {
    New-Item -ItemType Directory -Path '.reports' -Force | Out-Null
    # Sandbox and approved runs can have different access to the shared temp
    # directory. Each run gets a new directory inside this workspace.
    $taskTestTemp = Join-Path $taskRoot ('.reports\tmp-' + [Guid]::NewGuid().ToString('N'))
    & $taskPython -m pytest @TestPaths -q '--junitxml=.reports/tests.xml' "--basetemp=$taskTestTemp" -o "cache_dir=$taskTestTemp-cache"
    $taskExitCode = $LASTEXITCODE
    if ($taskExitCode -ne 0) { exit $taskExitCode }
    & $taskPython -m pip check
    exit $LASTEXITCODE
} finally { Pop-Location }
