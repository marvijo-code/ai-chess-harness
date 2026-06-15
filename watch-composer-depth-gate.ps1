[CmdletBinding()]
param(
  [int]$Depth = 8,
  [int]$ComposerMs = 4000,
  [int]$Port = 8879,
  [switch]$ComposerBlack,
  [switch]$NoAnalysis,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSCommandPath
$ViewerScript = Join-Path $RepoRoot 'tools\live_pgn_viewer.py'
$MatchScript = Join-Path $RepoRoot 'tools\run_composer_depth_gate.py'
$BaseUrl = "http://127.0.0.1:$Port"

function Stop-ComposerDepthViewerOnPort {
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
$Slug = "composer-vs-stockfish-depth-$Depth-$Stamp"
$LivePgnPath = Join-Path $RepoRoot "out\live\$Slug-live.pgn"
$LiveHash = [uri]::EscapeDataString("$Slug--live-game-1")
$LiveUrl = "$BaseUrl/#$LiveHash"

$Python = (Get-Command python -ErrorAction Stop).Source
Stop-ComposerDepthViewerOnPort -TargetPort $Port
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
  $stderrTail = ''
  if (Test-Path -LiteralPath $ViewerStderr) {
    $stderrTail = (Get-Content -LiteralPath $ViewerStderr -Tail 20 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
  }
  throw "Viewer did not start on $BaseUrl`n$stderrTail"
}

Write-Host "Live viewer: $LiveUrl"
Write-Host "Live PGN: $LivePgnPath"
$UrlFile = Join-Path $LiveDir 'composer-live.url'
$LiveUrl | Set-Content -Path $UrlFile -Encoding utf8
Write-Host "Bookmark file: $UrlFile"
if (-not $NoOpen) {
  Start-Process $LiveUrl
}

$MatchArgs = @(
  $MatchScript,
  '--depth', "$Depth",
  '--composer-ms', "$ComposerMs",
  '--live-pgn', $LivePgnPath,
  '--viewer-port', "$Port"
)
if ($ComposerBlack) {
  $MatchArgs += '--composer-black'
}

& $Python @MatchArgs
$ExitCode = $LASTEXITCODE

$ArchivePgn = Get-ChildItem -Path (Join-Path $RepoRoot 'out\composer-depth-matches') -Filter "composer-vs-stockfish-depth-$Depth-*.pgn" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($ArchivePgn) {
  $ArchiveSlug = $ArchivePgn.BaseName
  $ArchiveUrl = "$BaseUrl/#$([uri]::EscapeDataString("$ArchiveSlug--game-1"))"
  Write-Host "Archive URL: $ArchiveUrl"
}

Write-Host "Follow live: $LiveUrl"
exit $ExitCode
