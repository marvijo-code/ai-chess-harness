[CmdletBinding()]
param(
  [int]$Port = 8879,
  [switch]$StartNew,
  [int]$Depth = 1,
  [int]$ComposerMs = 4000,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$LiveDir = Join-Path $RepoRoot 'out\live'
$UrlFile = Join-Path $LiveDir 'composer-live.url'

function Get-LatestComposerLivePgn {
  Get-ChildItem -Path $LiveDir -Filter 'composer-vs-stockfish-depth-*-live.pgn' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
}

if ($StartNew) {
  & (Join-Path $RepoRoot 'watch-composer-depth-gate.ps1') -Depth $Depth -ComposerMs $ComposerMs -Port $Port -NoOpen:(!(-not $NoOpen))
  exit $LASTEXITCODE
}

$latest = Get-LatestComposerLivePgn
if ($null -eq $latest) {
  Write-Host 'No composer live PGN yet. Starting a new depth gate match...'
  & (Join-Path $RepoRoot 'watch-composer-depth-gate.ps1') -Depth $Depth -ComposerMs $ComposerMs -Port $Port -NoOpen:$NoOpen
  exit $LASTEXITCODE
}

$slug = $latest.BaseName
if ($slug.EndsWith('-live')) {
  $slug = $slug.Substring(0, $slug.Length - 5)
}
$LiveUrl = "http://127.0.0.1:$Port/#$slug--live-game-1"
$LiveUrl | Set-Content -Path $UrlFile -Encoding utf8

Write-Host "Composer live board: $LiveUrl"
Write-Host "Live PGN: $($latest.FullName)"
Write-Host "URL file: $UrlFile"

if (-not $NoOpen) {
  Start-Process $LiveUrl
}

exit 0
