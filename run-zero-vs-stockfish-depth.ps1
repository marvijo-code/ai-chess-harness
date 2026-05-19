[CmdletBinding()]
param(
  [int]$Depth = 1,
  [int]$Port = 8877,
  [switch]$NoAnalysis,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSCommandPath
$ViewerScript = Join-Path $RepoRoot 'tools\live_pgn_viewer.py'
$BaseUrl = "http://127.0.0.1:$Port"

function Stop-DepthViewerOnPort {
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
    if ($processId -le 0) {
      continue
    }
    $process = $allProcesses | Where-Object { [int]$_.ProcessId -eq $processId } | Select-Object -First 1
    if ($null -eq $process -or [string]::IsNullOrWhiteSpace($process.CommandLine)) {
      continue
    }
    if ($process.CommandLine -like "*live_pgn_viewer.py*" -and $process.CommandLine -like "*$RepoRoot*") {
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
  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    $remaining = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
    if (-not $remaining) {
      return
    }
  }
  $remainingViewer = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
  if ($remainingViewer) {
    throw "Could not stop existing viewer on port $TargetPort"
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
$Slug = "zero-vs-stockfish-depth-$Depth-$Stamp"
$LivePgnPath = Join-Path $RepoRoot "out\live\$Slug-live.pgn"
$LiveHash = [uri]::EscapeDataString("$Slug--live-game-1")
$LiveUrl = "$BaseUrl/#$LiveHash"

$Python = (Get-Command python -ErrorAction Stop).Source
Stop-DepthViewerOnPort -TargetPort $Port
Start-Sleep -Milliseconds 300

$LiveDir = Join-Path $RepoRoot 'out\live'
if (-not (Test-Path -LiteralPath $LiveDir)) {
  New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null
}
$ViewerStdout = Join-Path $LiveDir "$Slug-viewer.out.log"
$ViewerStderr = Join-Path $LiveDir "$Slug-viewer.err.log"

$ViewerArgs = @($ViewerScript, '--pgn', $LivePgnPath, '--port', "$Port", '--stats-dir', (Join-Path $RepoRoot 'out'))
if ($NoAnalysis) {
  $ViewerArgs += '--no-analysis'
}
# Redirect stdio so the viewer does not inherit this script's standard handles
# (otherwise PowerShell waits on those handles and the prompt never returns).
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

$Body = @{
  depth = $Depth
  stamp = $Stamp
  live_pgn_path = $LivePgnPath
} | ConvertTo-Json -Compress
$MatchJob = Start-Job -ScriptBlock {
  param([string]$Uri, [string]$RequestBody)
  Invoke-RestMethod -Method Post -Uri $Uri -ContentType 'application/json' -Body $RequestBody -TimeoutSec 300
} -ArgumentList "$BaseUrl/api/research/depth-match", $Body

if (-not $NoOpen) {
  Start-Process $LiveUrl
}

try {
  $CompletedJob = Wait-Job -Job $MatchJob -Timeout 300
  if ($null -eq $CompletedJob) {
    throw "Depth match did not finish within 300 seconds. Live URL: $LiveUrl"
  }

  $Result = Receive-Job -Job $MatchJob -ErrorAction Stop
  if ($Result -is [array]) {
    $Result = $Result[-1]
  }
  if (-not $Result.ok) {
    throw "Depth match failed: $($Result.error)"
  }
}
finally {
  Stop-Job -Job $MatchJob -ErrorAction SilentlyContinue
  Remove-Job -Job $MatchJob -Force -ErrorAction SilentlyContinue
}

$Hash = [uri]::EscapeDataString("$($Result.tournament_slug)--game-1")
$Url = "$BaseUrl/#$Hash"

Write-Host "Isolated Zero vs Stockfish depth $Depth complete."
Write-Host "PGN: $($Result.pgn_path)"
Write-Host "Live PGN: $($Result.live_pgn_path)"
Write-Host "URL: $LiveUrl"
Write-Host "Archive URL: $Url"

exit 0
