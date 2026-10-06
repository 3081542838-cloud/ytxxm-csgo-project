# Work around interrupted large transfers. Same official bytes, SHA256 verified.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskCache = Join-Path $taskRoot '.wheels'
New-Item -ItemType Directory -Path $taskCache -Force | Out-Null
$taskRelease = Invoke-RestMethod 'https://pypi.org/pypi/PySide6-Essentials/6.11.2/json' -TimeoutSec 30
$taskWheel = $taskRelease.urls | Where-Object filename -EQ 'pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl'
if (-not $taskWheel) { throw 'Official Windows wheel missing.' }
if (([uri]$taskWheel.url).Host -ne 'files.pythonhosted.org') { throw 'Unexpected download host.' }
$taskTarget = Join-Path $taskCache $taskWheel.filename
$taskExisting = if (Test-Path -LiteralPath $taskTarget) { (Get-Item -LiteralPath $taskTarget).Length } else { 0 }
if ($taskExisting -gt $taskWheel.size) { throw 'Existing file exceeds expected size.' }
if ($taskExisting -lt $taskWheel.size) {
    Write-Output "Resume official wheel at $taskExisting / $($taskWheel.size) bytes"
    $taskStatus = & curl.exe --fail --location --silent --show-error --connect-timeout 20 --max-time 1200 --speed-time 120 --speed-limit 512 --continue-at - --output $taskTarget --write-out '%{http_code}' $taskWheel.url
    if ($LASTEXITCODE -ne 0 -or $taskStatus -notin @('200','206')) { throw "Download incomplete; retained partial file: $taskStatus" }
}
if ((Get-Item -LiteralPath $taskTarget).Length -ne $taskWheel.size) { throw 'Incomplete wheel length.' }
$taskHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $taskTarget).Hash.ToLowerInvariant()
if ($taskHash -ne $taskWheel.digests.sha256) { throw 'Official SHA256 mismatch.' }
$taskWheel | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $taskCache 'qt-official-metadata.json') -Encoding utf8
Write-Output "Official SHA256 verified: $taskHash"
