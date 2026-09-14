[CmdletBinding()]
param(
  [string]$WhiteModel = 'z-ai/glm-5.3-flash',
  [string]$BlackModel = 'deepseek/deepseek-v4.1-flash',
  [int]$Port = 8766,
  [int]$TimeControlMs = 600000,
  [int]$IncrementMs = 5000,
  [int]$MaxAttempts = 3,
  [int]$MaxPlies = 60,
  [int]$AttemptTimeoutSeconds = 90,
  [string]$WhiteReasoningEffort = 'low',
  [string]$BlackReasoningEffort = 'minimal',
  [string]$Slug,
  [switch]$Open
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$LiveDir = Join-Path $RepoRoot 'out\live'
if (-not (Test-Path -LiteralPath $LiveDir)) {
  New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null
}
$Python = (Get-Command python -ErrorAction Stop).Source

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $Slug) {
  $whiteSlug = ($WhiteModel -replace '[^A-Za-z0-9]', '')
  $blackSlug = ($BlackModel -replace '[^A-Za-z0-9]', '')
  $Slug = "openrouter-$whiteSlug-vs-$blackSlug-$stamp"
}

$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($connection in $connections) {
  Stop-Process -Id ([int]$connection.OwningProcess) -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 600

$LivePgn = Join-Path $LiveDir "$Slug-live.pgn"
$ViewerOut = Join-Path $LiveDir "$Slug-viewer.out.log"
$ViewerErr = Join-Path $LiveDir "$Slug-viewer.err.log"
$MatchOut = Join-Path $LiveDir "$Slug-match.out.log"
$MatchErr = Join-Path $LiveDir "$Slug-match.err.log"
$ViewerCmd = Join-Path $LiveDir "$Slug-viewer.cmd"
$MatchCmd = Join-Path $LiveDir "$Slug-match.cmd"

$viewerLine = '"{0}" "{1}\tools\live_pgn_viewer.py" --pgn "{2}" --port {3} --stats-dir "{1}\out" 1> "{4}" 2> "{5}"' -f `
  $Python, $RepoRoot, $LivePgn, $Port, $ViewerOut, $ViewerErr
Set-Content -LiteralPath $ViewerCmd -Value @('@echo off', $viewerLine) -Encoding ASCII

$attemptMs = $AttemptTimeoutSeconds * 1000
$matchArgLine = '--openrouter-model {0} --black-openrouter-model {1} --white-movetime-ms {2} --black-movetime-ms {2} --max-attempts {3} --max-plies {4} --time-control-ms {5} --increment-ms {6} --white-env "OPENROUTER_TIMEOUT_SECONDS={8}" --black-env "OPENROUTER_TIMEOUT_SECONDS={8}" --white-env "OPENROUTER_REASONING_EFFORT={9}" --black-env "OPENROUTER_REASONING_EFFORT={10}" --live-pgn "{7}" --event "OpenRouter LLM exhibition"' -f `
  $WhiteModel, $BlackModel, $attemptMs, $MaxAttempts, $MaxPlies, $TimeControlMs, $IncrementMs, $LivePgn, $AttemptTimeoutSeconds, $WhiteReasoningEffort, $BlackReasoningEffort
$matchLine = '"{0}" "{1}\tools\play_engine_match.py" {2} 1> "{3}" 2> "{4}"' -f `
  $Python, $RepoRoot, $matchArgLine, $MatchOut, $MatchErr
Set-Content -LiteralPath $MatchCmd -Value @('@echo off', $matchLine) -Encoding ASCII

Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd.exe /c `"$ViewerCmd`"" } | Out-Null

$BaseUrl = "http://127.0.0.1:$Port"
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
  Start-Sleep -Milliseconds 500
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/api/research" -TimeoutSec 2 | Out-Null
    $ready = $true
    break
  } catch {
  }
}
if (-not $ready) {
  throw "Viewer did not start on $BaseUrl; see $ViewerErr"
}

Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd.exe /c `"$MatchCmd`"" } | Out-Null

$hash = [uri]::EscapeDataString("$Slug--live-game-1")
$url = "$BaseUrl/#$hash"
if ($Open) {
  Start-Process $url
}
Write-Host "Live URL: $url"
Write-Host "Live PGN: $LivePgn"
Write-Host "Match log: $MatchOut"