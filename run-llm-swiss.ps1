<#
.SYNOPSIS
  One-command LLM Swiss tournament with live Elo and a live web view.

.DESCRIPTION
  Players and rules come from configs\llm-swiss-ai-players.json (or -Config). The
  script asks every player for one real move first (same prompt, clock and rules as
  the tournament), then starts the tournament viewer and the runner as detached
  processes and prints the live URL. It only ever stops a viewer it started.

.EXAMPLE
  .\run-llm-swiss.ps1
.EXAMPLE
  .\run-llm-swiss.ps1 -Rounds 3 -Port 8771
#>
[CmdletBinding()]
param(
  [string]$Config,
  [int]$Rounds = 0,
  [int]$Port = 8770,
  [string]$Resume,
  [switch]$NoPreflight,
  [switch]$Open
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$LiveDir = Join-Path $RepoRoot 'out\live'
New-Item -ItemType Directory -Path $LiveDir -Force | Out-Null
$Python = (Get-Command python -ErrorAction Stop).Source
$Runner = Join-Path $RepoRoot 'tools\play_llm_swiss.py'
if (-not $Config) { $Config = Join-Path $RepoRoot 'configs\llm-swiss-ai-players.json' }

$runArgs = @()
if ($Resume) { $runArgs += @('--resume', $Resume) } else { $runArgs += @('--config', $Config) }
if ($Rounds -gt 0) { $runArgs += @('--rounds', $Rounds) }

if (-not $NoPreflight) {
  Write-Host 'Preflight: asking every player for one real move...'
  & $Python $Runner --preflight-only @runArgs
  if ($LASTEXITCODE -ne 0) { throw 'Preflight failed - see the PREFLIGHT FAILED lines above. Nothing was started.' }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Slug = if ($Resume) { (Split-Path -Leaf $Resume) -replace '-tournament\.json$', '' } else { "llm-swiss-$stamp" }
$State = Join-Path $LiveDir "$Slug-tournament.json"

# Reuse the port only when the listener is the viewer this script started last time.
$PidFile = Join-Path $LiveDir "llm-swiss-viewer-$Port.pid"
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
    $PidFile = Join-Path $LiveDir "llm-swiss-viewer-$Port.pid"
  }
}

function Start-Detached([string]$Name, [string]$Line) {
  $cmdFile = Join-Path $LiveDir "$Slug-$Name.cmd"
  Set-Content -LiteralPath $cmdFile -Value @('@echo off', $Line) -Encoding ASCII
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd.exe /c `"$cmdFile`"" } | Out-Null
}

$viewerLine = '"{0}" "{1}\tools\llm_tournament_viewer.py" --port {2} --state "{3}" 1> "{4}" 2> "{5}"' -f `
  $Python, $RepoRoot, $Port, $State, (Join-Path $LiveDir "$Slug-viewer.out.log"), (Join-Path $LiveDir "$Slug-viewer.err.log")
Start-Detached 'viewer' $viewerLine
$BaseUrl = "http://127.0.0.1:$Port"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  try { Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/api/viewer-version" -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { }
}
if (-not $ready) { throw "Viewer did not start on $BaseUrl; see $LiveDir\$Slug-viewer.err.log" }
$viewerPid = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
Set-Content -LiteralPath $PidFile -Value $viewerPid -Encoding ASCII

$quoted = ($runArgs | ForEach-Object { if ("$_" -match '\s') { '"' + $_ + '"' } else { "$_" } }) -join ' '
$slugArg = if ($Resume) { '' } else { "--slug $Slug" }
$matchLine = '"{0}" "{1}" --no-preflight {2} {3} 1> "{4}" 2> "{5}"' -f `
  $Python, $Runner, $slugArg, $quoted, (Join-Path $LiveDir "$Slug-tournament.out.log"), (Join-Path $LiveDir "$Slug-tournament.err.log")
Start-Detached 'runner' $matchLine

if ($Open) { Start-Process $BaseUrl }
Write-Host ''
Write-Host "Tournament: $Slug"
Write-Host "Live URL:   $BaseUrl/"
Write-Host "Runner log: $LiveDir\$Slug-tournament.out.log"
Write-Host "State:      $State"
Write-Host "Archive:    $RepoRoot\out\llm-tournaments\$Slug.pgn (after each round)"
