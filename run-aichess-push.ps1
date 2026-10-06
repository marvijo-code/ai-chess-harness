<#
.SYNOPSIS
  Publish the local AI-chess tournament viewer to the public relay (start, stop, status).

.DESCRIPTION
  Starts two hidden, self-restarting processes (no console windows):
    1. an ssh forward tunnel  127.0.0.1:18781 (laptop) -> 127.0.0.1:8781 (relay ingest on the VPS)
    2. tools\aichess_push.py, which reads the local viewer (http://127.0.0.1:8770) and posts
       its live data through the tunnel.
  Both run as "cmd.exe /c <file>.cmd" loops started through tools\run-hidden-cmd.vbs. Their
  cmd.exe PIDs go to out\aichess-push\*.pid; -Stop stops ONLY those PIDs and their children.
  The VPS login comes from -Vps, else the user environment variable ROUND_RECORDER_VPS, else the
  gitignored out\aichess-push\vps.txt saved by the last run that passed -Vps.
  The ingest token is the file ~\.aichess-ingest-token (created when missing, never printed).
  The relay itself is deployed with tools\aichess_relay_vps\deploy.ps1.

.EXAMPLE
  .\run-aichess-push.ps1 -Start
.EXAMPLE
  .\run-aichess-push.ps1 -Status
.EXAMPLE
  .\run-aichess-push.ps1 -Stop
#>
[CmdletBinding()]
param(
  [switch]$Start,
  [switch]$Stop,
  [switch]$Status,
  [string]$Vps,
  [string]$LiveDir = 'C:\dev\chess-harness-codex\out\live',
  [string]$LocalUrl = 'http://127.0.0.1:8770',
  [int]$TunnelPort = 18781,
  [int]$RemoteIngestPort = 8781,
  [string]$TokenFile = (Join-Path $HOME '.aichess-ingest-token')
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$StateDir = Join-Path $RepoRoot 'out\aichess-push'
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
$Hidden = Join-Path $RepoRoot 'tools\run-hidden-cmd.vbs'

# user@host of the VPS is kept out of this public repo: -Vps, else ROUND_RECORDER_VPS, else the
# gitignored out\aichess-push\vps.txt remembered from the last explicit -Vps.
$VpsFile = Join-Path $StateDir 'vps.txt'
if ($Vps) { Set-Content -LiteralPath $VpsFile -Value $Vps -Encoding ASCII }
if (-not $Vps) { $Vps = [Environment]::GetEnvironmentVariable('ROUND_RECORDER_VPS', 'User') }
if (-not $Vps -and (Test-Path -LiteralPath $VpsFile)) { $Vps = (Get-Content -LiteralPath $VpsFile -Raw).Trim() }
if (-not $Vps -and $Start) { throw 'Pass -Vps user@host or set ROUND_RECORDER_VPS (the VPS is not stored in the repo).' }

function Initialize-Token {
  if (-not (Test-Path -LiteralPath $TokenFile)) {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $hex = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    [IO.File]::WriteAllText($TokenFile, $hex)
    Write-Host "created ingest token file $TokenFile (64 hex characters)"
  }
}

function Get-OwnProcess([string]$Name) {
  # The cmd.exe loop this launcher started, verified by its command line (never trust a bare PID).
  $pidFile = Join-Path $StateDir "$Name.pid"
  if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
  $id = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
  $cmdFile = Join-Path $StateDir "$Name.cmd"
  if ($p -and $p.Name -eq 'cmd.exe' -and $p.CommandLine -like "*$cmdFile*") { return $p }
  return $null
}

function Get-Descendants([int]$ParentId) {
  $all = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId" -ErrorAction SilentlyContinue
  foreach ($child in $all) {
    $child
    Get-Descendants $child.ProcessId
  }
}

function Start-Hidden([string]$Name, [string[]]$Lines) {
  $own = Get-OwnProcess $Name
  if ($own) { Write-Host "$Name already running, PID $($own.ProcessId)"; return }
  $cmdFile = Join-Path $StateDir "$Name.cmd"
  Set-Content -LiteralPath $cmdFile -Value (@('@echo off') + $Lines) -Encoding ASCII
  Start-Process wscript.exe -ArgumentList '//B', '//Nologo', "`"$Hidden`"", "`"$cmdFile`""
  $p = $null
  for ($i = 0; $i -lt 30 -and -not $p; $i++) {
    Start-Sleep -Milliseconds 300
    $p = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -like "*$cmdFile*" } | Select-Object -First 1
  }
  if (-not $p) { throw "$Name did not start (see $StateDir)" }
  Set-Content -LiteralPath (Join-Path $StateDir "$Name.pid") -Value $p.ProcessId -Encoding ASCII
  Write-Host "$Name started, PID $($p.ProcessId) (pid file out\aichess-push\$Name.pid)"
}

function Stop-Own([string]$Name) {
  $pidFile = Join-Path $StateDir "$Name.pid"
  $own = Get-OwnProcess $Name
  if (-not $own) {
    Write-Host "$Name is not running (no matching PID)"
  } else {
    $kids = @(Get-Descendants $own.ProcessId)
    Stop-Process -Id $own.ProcessId -Confirm:$false -ErrorAction SilentlyContinue   # the loop first, so it cannot restart
    foreach ($k in $kids) {
      if ($k.Name -ne 'conhost.exe') { Stop-Process -Id $k.ProcessId -Confirm:$false -ErrorAction SilentlyContinue }
    }
    Write-Host "$Name stopped (PID $($own.ProcessId) and $($kids.Count) child process(es))"
  }
  if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Confirm:$false }
}

function Show-Status {
  foreach ($name in 'tunnel', 'pusher') {
    $own = Get-OwnProcess $name
    if ($own) {
      $kids = @(Get-Descendants $own.ProcessId | Where-Object { $_.Name -ne 'conhost.exe' } | ForEach-Object { "$($_.Name):$($_.ProcessId)" })
      Write-Host "$name running: cmd PID $($own.ProcessId), children $($kids -join ', ')"
    } else { Write-Host "$name not running" }
  }
  $statusFile = Get-ChildItem -LiteralPath $LiveDir -Filter '*-publish-status.json' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($statusFile) { Write-Host "status file $($statusFile.FullName):"; Get-Content -LiteralPath $statusFile.FullName -Raw | Write-Host }
  try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$TunnelPort/healthz" -TimeoutSec 5
    Write-Host "relay (through tunnel): $($h | ConvertTo-Json -Compress)"
  } catch { Write-Host "relay not reachable through the tunnel: $($_.Exception.Message)" }
}

if ($Stop) {
  Stop-Own 'pusher'
  Stop-Own 'tunnel'
}

if ($Start) {
  Initialize-Token
  $python = (& python -c "import sys; print(sys.executable)").Trim()
  $tunnelLog = Join-Path $StateDir 'tunnel.log'
  $pushLog = Join-Path $StateDir 'pusher.log'
  Start-Hidden 'tunnel' @(
    ':loop',
    "ssh -N -L $($TunnelPort):127.0.0.1:$($RemoteIngestPort) -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o BatchMode=yes -o ConnectTimeout=20 $Vps >> `"$tunnelLog`" 2>&1",
    'ping -n 6 127.0.0.1 >nul',
    'goto loop'
  )
  Start-Hidden 'pusher' @(
    "cd /d `"$RepoRoot`"",
    ':loop',
    "`"$python`" tools\aichess_push.py --local $LocalUrl --relay http://127.0.0.1:$TunnelPort --token-file `"$TokenFile`" --live-dir `"$LiveDir`" >> `"$pushLog`" 2>&1",
    'ping -n 6 127.0.0.1 >nul',
    'goto loop'
  )
  Write-Host "logs: $tunnelLog, $pushLog"
}

if ($Status -or -not ($Start -or $Stop)) { Show-Status }
