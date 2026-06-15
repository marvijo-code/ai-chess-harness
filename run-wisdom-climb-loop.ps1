[CmdletBinding()]
param(
  [int]$WisdomMs = 15000,
  [int]$TargetDepth = 8,
  [int]$Port = 8880,
  [switch]$Reset,
  [switch]$NoAnalysis,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$LoopScript = Join-Path $RepoRoot 'tools\run_wisdom_depth_climb_loop.py'
$ViewerScript = Join-Path $RepoRoot 'tools\live_pgn_viewer.py'
$BaseUrl = "http://127.0.0.1:$Port"

function Stop-WisdomViewerOnPort {
  param([int]$TargetPort)
  @(Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue) | ForEach-Object {
    $pid = [int]$_.OwningProcess
    if ($pid -gt 0) { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue }
  }
}

function Test-ViewerReady {
  param([string]$Url)
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/viewer-version" -TimeoutSec 5 | Out-Null
    return $true
  } catch { return $false }
}

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Slug = "wisdom-climb-loop-$Stamp"
$LivePgnPath = Join-Path $RepoRoot "out\live\$Slug-live.pgn"
$LiveUrl = "$BaseUrl/#$([uri]::EscapeDataString("$Slug--live-game-1"))"
$Python = (Get-Command python -ErrorAction Stop).Source

Stop-WisdomViewerOnPort -TargetPort $Port
Start-Sleep -Milliseconds 300

$LiveDir = Join-Path $RepoRoot 'out\live'
if (-not (Test-Path $LiveDir)) { New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null }
New-Item -ItemType File -Path $LivePgnPath -Force | Out-Null

$ViewerArgs = @($ViewerScript, '--pgn', $LivePgnPath, '--port', "$Port", '--stats-dir', (Join-Path $RepoRoot 'out'))
if ($NoAnalysis) { $ViewerArgs += '--no-analysis' }
Start-Process -FilePath $Python -ArgumentList $ViewerArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null

for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  if (Test-ViewerReady -Url $BaseUrl) { break }
}

Write-Host "Live viewer: $LiveUrl"
Write-Host "Loop log: out\wisdom-depth-matches\climb-loop-log.jsonl"
Write-Host "State: out\wisdom-depth-matches\depth-climb-state.json"
$LiveUrl | Set-Content -Path (Join-Path $LiveDir 'wisdom-climb-loop.url') -Encoding utf8
if (-not $NoOpen) { Start-Process $LiveUrl }

$LoopArgs = @($LoopScript, '--wisdom-ms', "$WisdomMs", '--target-depth', "$TargetDepth", '--live-pgn', $LivePgnPath)
if ($Reset) { $LoopArgs += '--reset' }

& $Python @LoopArgs
exit $LASTEXITCODE
