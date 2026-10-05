<#
.SYNOPSIS
  One-command LLM vs LLM series (default: GPT-6 Sol high vs Sonnet 5.5 high, best of 3).

.DESCRIPTION
  Defaults come from the llmMatch section of chess-harness.config.json. Players are
  provider:model:effort specs: codex (ChatGPT subscription via codex exec), claude
  (Claude subscription via claude -p) or openrouter (metered API credit).
  The script probes one move per player, then starts the viewer and the series runner
  as detached processes and prints the live URL. It only ever stops a viewer it started.

.EXAMPLE
  .\run-llm-match.ps1
.EXAMPLE
  .\run-llm-match.ps1 -Player1 codex:gpt-6.1-sol:xhigh -Player1Name "GPT-6.1 Sol (xhigh)" -Games 5
#>
[CmdletBinding()]
param(
  [string]$Player1,
  [string]$Player1Name,
  [string]$Player2,
  [string]$Player2Name,
  [int]$Games = 0,
  [int]$Port = 0,
  [int]$TimeControlMs = 0,
  [int]$IncrementMs = -1,
  [switch]$PlayAll,
  [switch]$NoPreflight,
  [switch]$Open
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$LiveDir = Join-Path $RepoRoot 'out\live'
New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null
$Python = (Get-Command python -ErrorAction Stop).Source
$Config = Get-Content -LiteralPath (Join-Path $RepoRoot 'chess-harness.config.json') -Raw | ConvertFrom-Json
if ($Port -le 0) { $Port = if ($Config.llmMatch.port) { [int]$Config.llmMatch.port } else { 8768 } }

$playerArgs = @()
if ($Player1) { $playerArgs += @('--player1', $Player1) }
if ($Player1Name) { $playerArgs += @('--player1-name', $Player1Name) }
if ($Player2) { $playerArgs += @('--player2', $Player2) }
if ($Player2Name) { $playerArgs += @('--player2-name', $Player2Name) }
$runArgs = @() + $playerArgs
if ($Games -gt 0) { $runArgs += @('--games', $Games) }
if ($TimeControlMs -gt 0) { $runArgs += @('--time-control-ms', $TimeControlMs) }
if ($IncrementMs -ge 0) { $runArgs += @('--increment-ms', $IncrementMs) }
if ($PlayAll) { $runArgs += '--play-all' }

$Series = Join-Path $RepoRoot 'tools\play_llm_series.py'
if (-not $NoPreflight) {
  Write-Host 'Preflight: asking each player for one opening move...'
  & $Python $Series --preflight-only @playerArgs
  if ($LASTEXITCODE -ne 0) {
    throw 'Preflight failed - see the PREFLIGHT FAILED lines above. Nothing was started.'
  }
}

# Reuse the port only when the listener is the viewer this script started last time.
$PidFile = Join-Path $LiveDir "llm-match-viewer-$Port.pid"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
  $ownPid = if (Test-Path -LiteralPath $PidFile) { [int](Get-Content -LiteralPath $PidFile -Raw) } else { 0 }
  if ($ownPid -and [int]$listener.OwningProcess -eq $ownPid) {
    Stop-Process -Id $ownPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 600
  } else {
    $free = $Port + 1
    while (Get-NetTCPConnection -LocalPort $free -State Listen -ErrorAction SilentlyContinue) { $free++ }
    Write-Host "Port $Port is used by another process; using $free instead."
    $Port = $free
    $PidFile = Join-Path $LiveDir "llm-match-viewer-$Port.pid"
  }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$p1 = if ($Player1Name) { $Player1Name } elseif ($Player1) { $Player1 } else { $Config.llmMatch.player1.name }
$p2 = if ($Player2Name) { $Player2Name } elseif ($Player2) { $Player2 } else { $Config.llmMatch.player2.name }
$short = { param($s) $x = ($s.ToLower() -replace '[^a-z0-9]', ''); if (-not $x) { $x = 'player' }; $x.Substring(0, [Math]::Min(20, $x.Length)) }
$Slug = "llm-match-$(& $short $p1)-vs-$(& $short $p2)-$stamp"
$LivePgn = Join-Path $LiveDir "$Slug-live.pgn"

function Start-Detached([string]$Name, [string]$Line) {
  $cmdFile = Join-Path $LiveDir "$Slug-$Name.cmd"
  Set-Content -LiteralPath $cmdFile -Value @('@echo off', $Line) -Encoding ASCII
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd.exe /c `"$cmdFile`"" } | Out-Null
}

$viewerLine = '"{0}" "{1}\tools\live_pgn_viewer.py" --pgn "{2}" --port {3} --stats-dir "{1}\out" 1> "{4}" 2> "{5}"' -f `
  $Python, $RepoRoot, $LivePgn, $Port, (Join-Path $LiveDir "$Slug-viewer.out.log"), (Join-Path $LiveDir "$Slug-viewer.err.log")
Start-Detached 'viewer' $viewerLine

$BaseUrl = "http://127.0.0.1:$Port"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/api/viewer-version" -TimeoutSec 2 | Out-Null
    $ready = $true
    break
  } catch { }
}
if (-not $ready) { throw "Viewer did not start on $BaseUrl; see $LiveDir\$Slug-viewer.err.log" }
$viewerPid = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
Set-Content -LiteralPath $PidFile -Value $viewerPid -Encoding ASCII

$quoted = ($runArgs | ForEach-Object { if ("$_" -match '\s') { '"' + $_ + '"' } else { "$_" } }) -join ' '
$matchLine = '"{0}" "{1}" --no-preflight --slug {2} --live-pgn "{3}" {4} 1> "{5}" 2> "{6}"' -f `
  $Python, $Series, $Slug, $LivePgn, $quoted, (Join-Path $LiveDir "$Slug-match.out.log"), (Join-Path $LiveDir "$Slug-match.err.log")
Start-Detached 'match' $matchLine

$url = "$BaseUrl/#" + [uri]::EscapeDataString("$Slug--live-game-1")
if ($Open) { Start-Process $url }
Write-Host ''
Write-Host "Series:    $p1 vs $p2"
Write-Host "Live URL:  $url"
Write-Host "Match log: $LiveDir\$Slug-match.out.log"
Write-Host "Archive:   $RepoRoot\out\llm-matches\$Slug.pgn (written after each game)"
