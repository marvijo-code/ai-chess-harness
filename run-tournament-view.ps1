<#
.SYNOPSIS
  Show any LLM match in the tournament UI on the fixed local port 8770.

.DESCRIPTION
  One command, one port, always the tournament view. The bridge mirrors the
  match PGN into tournament state and the viewer serves it on 8770.

  With no argument the newest out/live/*-live.pgn is used.

.EXAMPLE
  .\run-tournament-view.ps1
.EXAMPLE
  .\run-tournament-view.ps1 -Slug deepseek-sharedtools-vs-sf4-20261026-130324
#>
[CmdletBinding()]
param(
  [string]$Slug,
  [int]$Port = 8770,
  [switch]$NoAnalysis,
  [switch]$Open
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$Python = (Get-Command python -ErrorAction Stop).Source
$LiveDir = Join-Path $RepoRoot 'out\live'

if (-not $Slug) {
  $newest = Get-ChildItem -LiteralPath $LiveDir -Filter '*-live.pgn' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $newest) { throw "No *-live.pgn in $LiveDir yet." }
  $Slug = $newest.Name -replace '-live\.pgn$', ''
}

$LivePgn = Join-Path $LiveDir "$Slug-live.pgn"
$State = Join-Path $LiveDir "$Slug-tournament.json"
if (-not (Test-Path -LiteralPath $LivePgn)) { throw "No match PGN: $LivePgn" }

# One state file, refreshed from the PGN (safe to run while the game is live).
& $Python (Join-Path $RepoRoot 'tools\tourney_state_bridge.py') --live-pgn $LivePgn --state $State --once
if ($LASTEXITCODE -ne 0) { throw 'bridge failed' }

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
  $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  Write-Host "Port $Port is already in use by $owner (pid $($listener.OwningProcess))."
  Write-Host "Reusing it if it is a viewer for this state; otherwise stop that process or pass -Port."
} else {
  $args = @((Join-Path $RepoRoot 'tools\llm_tournament_viewer.py'), '--port', $Port, '--state', $State)
  if ($NoAnalysis) { $args += '--no-analysis' }
  $log = Join-Path $LiveDir "$Slug-tourney.log"
  $err = Join-Path $LiveDir "$Slug-tourney.err"
  $cmd = '"{0}" {1} 1> "{2}" 2> "{3}"' -f $Python, (($args | ForEach-Object { '"' + $_ + '"' }) -join ' '), $log, $err
  $cmdFile = Join-Path $LiveDir "$Slug-tourney.cmd"
  Set-Content -LiteralPath $cmdFile -Value @('@echo off', $cmd) -Encoding Ascii
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd.exe /c `"$cmdFile`"" } | Out-Null
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try { Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/viewer-version" -TimeoutSec 2 | Out-Null; break } catch { }
  }
}

$url = "http://127.0.0.1:$Port/"
Write-Host ''
Write-Host "Tournament view: $url"
Write-Host "Match:           $Slug"
Write-Host "State:           $State"
if ($Open) { Start-Process $url }
