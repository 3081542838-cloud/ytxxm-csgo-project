$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskCommit = Invoke-RestMethod 'https://api.github.com/repos/DrEAmSs59/CS2-insight-agent/commits/main' -TimeoutSec 30
$taskRef = $taskCommit.sha
if ($taskRef -notmatch '^[0-9a-f]{40}$') { throw 'Invalid source commit.' }
$taskDirectory = Join-Path $taskRoot "resources\private\reference-$taskRef"
New-Item -ItemType Directory -Path $taskDirectory -Force | Out-Null
$taskFiles = @('pov/pov_default.vpk','pov/pov_voice_template.vpk','pov/README.md','pov/voice_hud_injection.js','LICENSE','THIRD_PARTY_LICENSES.md')
$taskRecords = @()
foreach ($taskPath in $taskFiles) {
    $taskInfo = Invoke-RestMethod "https://api.github.com/repos/DrEAmSs59/CS2-insight-agent/contents/$taskPath`?ref=$taskRef" -TimeoutSec 30
    if ($taskInfo.type -ne 'file' -or $taskInfo.size -gt 8MB) { throw "Unexpected or oversized resource: $taskPath" }
    $taskName = $taskPath.Replace('/','--')
    $taskTarget = Join-Path $taskDirectory $taskName
    $taskUrl = "https://raw.githubusercontent.com/DrEAmSs59/CS2-insight-agent/$taskRef/$taskPath"
    Invoke-WebRequest $taskUrl -OutFile $taskTarget -TimeoutSec 120
    if ((Get-Item -LiteralPath $taskTarget).Length -ne $taskInfo.size) { throw 'Resource length mismatch.' }
    $taskRecords += [PSCustomObject]@{Path=$taskPath;File=$taskName;Bytes=$taskInfo.size;SHA256=(Get-FileHash -Algorithm SHA256 -LiteralPath $taskTarget).Hash.ToLowerInvariant();Source=$taskUrl}
    Write-Output "Fetched $taskPath ($($taskInfo.size) bytes)"
}
[PSCustomObject]@{Repository='https://github.com/DrEAmSs59/CS2-insight-agent';Commit=$taskRef;Files=$taskRecords;DeploymentApproved=$false} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $taskDirectory 'source-manifest.json') -Encoding utf8
Write-Output "Source pinned at $taskRef; not approved for deployment."
