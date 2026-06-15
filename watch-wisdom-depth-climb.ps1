[CmdletBinding()]
param(
  [int]$StartDepth = 1,
  [int]$TargetDepth = 8,
  [int]$WisdomMs = 12000,
  [int]$Port = 8880,
  [int]$MaxAttempts = 0,
  [switch]$Reset,
  [switch]$NoAnalysis,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSCommandPath
$ViewerScript = Join-Path $RepoRoot 'tools\live_pgn_viewer.py'
$ClimbScript = Join-Path $RepoRoot 'tools\run_wisdom_depth_climb.py'
$BaseUrl = "http://127.0.0.1:$Port"

function Stop-WisdomViewerOnPort {
  param([int]$TargetPort)
  $connections = @(Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue)
  foreach ($connection in $connections) {
    $processId = [int]$connection.OwningProcess
    if ($processId -gt 0) {
      Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
  }
}

function Test-ViewerReady {
  param([string]$Url)
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/viewer-version" -TimeoutSec 5 | Out-Null
    return $true
  } catch {
    return $false
  }
}

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Slug = "wisdom-vs-stockfish-climb-$Stamp"
$LivePgnPath = Join-Path $RepoRoot "out\live\$Slug-live.pgn"
$LiveHash = [uri]::EscapeDataString("$Slug--live-game-1")
$LiveUrl = "$BaseUrl/#$LiveHash"

$Python = (Get-Command python -ErrorAction Stop).Source
Stop-WisdomViewerOnPort -TargetPort $Port
Start-Sleep -Milliseconds 300

$LiveDir = Join-Path $RepoRoot 'out\live'
if (-not (Test-Path -LiteralPath $LiveDir)) {
  New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null
}
New-Item -ItemType File -Path $LivePgnPath -Force | Out-Null
$ViewerStdout = Join-Path $LiveDir "$Slug-viewer.out.log"
$ViewerStderr = Join-Path $LiveDir "$Slug-viewer.err.log"

$ViewerArgs = @(
  $ViewerScript,
  '--pgn', $LivePgnPath,
  '--port', "$Port",
  '--stats-dir', (Join-Path $RepoRoot 'out')
)
if ($NoAnalysis) {
  $ViewerArgs += '--no-analysis'
}

$null = Start-Process -FilePath $Python -ArgumentList $ViewerArgs `
  -WorkingDirectory $RepoRoot -WindowStyle Hidden `
  -RedirectStandardOutput $ViewerStdout -RedirectStandardError $ViewerStderr

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
  Start-Sleep -Milliseconds 500
  if (Test-ViewerReady -Url $BaseUrl) {
    $ready = $true
    break
  }
}
if (-not $ready) {
  throw "Viewer did not start on $BaseUrl"
}

Write-Host "Live viewer: $LiveUrl"
Write-Host "Live PGN: $LivePgnPath"
$LiveUrl | Set-Content -Path (Join-Path $LiveDir 'wisdom-climb-live.url') -Encoding utf8
if (-not $NoOpen) {
  Start-Process $LiveUrl
}

$ClimbArgs = @(
  $ClimbScript,
  '--start-depth', "$StartDepth",
  '--target-depth', "$TargetDepth",
  '--wisdom-ms', "$WisdomMs",
  '--live-pgn', $LivePgnPath
)
if ($MaxAttempts -gt 0) {
  $ClimbArgs += @('--max-attempts', "$MaxAttempts")
}
if ($Reset) {
  $ClimbArgs += '--reset'
}

& $Python @ClimbArgs
exit $LASTEXITCODE
