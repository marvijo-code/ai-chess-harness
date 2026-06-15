[CmdletBinding()]
param(
  [int]$Games = 1,
  [int]$StockfishElo = 800,
  [int]$ComposerMs = 3000,
  [int]$StockfishMs = 150,
  [int]$Port = 8878,
  [switch]$NoEloLimit,
  [switch]$NoAnalysis,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSCommandPath
$ViewerScript = Join-Path $RepoRoot 'tools\live_pgn_viewer.py'
$MatchScript = Join-Path $RepoRoot 'tools\play_composer_vs_stockfish.py'
$BaseUrl = "http://127.0.0.1:$Port"

function Stop-ComposerViewerOnPort {
  param([int]$TargetPort)
  $connections = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
  $allProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
  $processesByParent = @{}
  foreach ($item in $allProcesses) {
    $parentId = [int]$item.ParentProcessId
    if (-not $processesByParent.ContainsKey($parentId)) {
      $processesByParent[$parentId] = @()
    }
    $processesByParent[$parentId] += $item
  }
  $processIdsToStop = New-Object System.Collections.Generic.HashSet[int]
  foreach ($connection in $connections) {
    $processId = [int]$connection.OwningProcess
    if ($processId -le 0) { continue }
    $process = $allProcesses | Where-Object { [int]$_.ProcessId -eq $processId } | Select-Object -First 1
    if ($null -eq $process -or [string]::IsNullOrWhiteSpace($process.CommandLine)) { continue }
    if ($process.CommandLine -like "*live_pgn_viewer.py*" -and $process.CommandLine -like "*--port $TargetPort*") {
      [void]$processIdsToStop.Add($processId)
      $queue = @($processId)
      while ($queue.Count -gt 0) {
        $current = [int]$queue[0]
        $queue = @($queue | Select-Object -Skip 1)
        foreach ($child in @($processesByParent[$current])) {
          $childId = [int]$child.ProcessId
          if ($processIdsToStop.Add($childId)) {
            $queue += $childId
          }
        }
      }
    }
  }
  foreach ($processId in @($processIdsToStop | Sort-Object -Descending)) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
  }
}

function Test-ViewerReady {
  param([string]$Url)
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/research" -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$EloTag = if ($NoEloLimit) { 'full' } else { "elo$StockfishElo" }
$Slug = "composer-vs-stockfish-$EloTag-$Stamp"
$LivePgnPath = Join-Path $RepoRoot "out\live\$Slug-live.pgn"
$LiveHash = [uri]::EscapeDataString("$Slug--live-game-1")
$LiveUrl = "$BaseUrl/#$LiveHash"

$Python = (Get-Command python -ErrorAction Stop).Source
Stop-ComposerViewerOnPort -TargetPort $Port
Start-Sleep -Milliseconds 300

$LiveDir = Join-Path $RepoRoot 'out\live'
if (-not (Test-Path -LiteralPath $LiveDir)) {
  New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null
}
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
for ($attempt = 0; $attempt -lt 30; $attempt++) {
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
if (-not $NoOpen) {
  Start-Process $LiveUrl
}

$MatchArgs = @(
  $MatchScript,
  '--games', "$Games",
  '--composer-ms', "$ComposerMs",
  '--stockfish-ms', "$StockfishMs",
  '--live-pgn', $LivePgnPath,
  '--viewer-port', "$Port"
)
if ($NoEloLimit) {
  $MatchArgs += '--no-elo-limit'
} else {
  $MatchArgs += @('--stockfish-elo', "$StockfishElo")
}

& $Python @MatchArgs
$ExitCode = $LASTEXITCODE

$ArchivePgn = Get-ChildItem -Path (Join-Path $RepoRoot 'out\composer-matches') -Filter "composer-vs-sf-$EloTag-*.pgn" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($ArchivePgn) {
  $ArchiveSlug = $ArchivePgn.BaseName
  $ArchiveUrl = "$BaseUrl/#$([uri]::EscapeDataString("$ArchiveSlug--game-1"))"
  Write-Host "Archive URL: $ArchiveUrl"
}

Write-Host "Follow live: $LiveUrl"
exit $ExitCode
