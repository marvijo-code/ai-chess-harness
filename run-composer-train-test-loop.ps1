[CmdletBinding()]
param(
  [int]$TargetDepth = 8,
  [int]$Games = 10,
  [double]$PassScore = 0.8,
  [int]$ComposerMs = 12000,
  [int]$MinChars = 200,
  [int]$Port = 8879,
  [switch]$Reset,
  [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath
$LoopScript = Join-Path $RepoRoot 'tools\run_composer_train_test_loop.py'
$ViewerScript = Join-Path $RepoRoot 'tools\live_pgn_viewer.py'
$BaseUrl = "http://127.0.0.1:$Port"

function Stop-ViewerOnPort {
  param([int]$TargetPort)
  @(Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue) | ForEach-Object {
    if ([int]$_.OwningProcess -gt 0) { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  }
}

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Slug = "composer-train-test-$Stamp"
$LivePgn = Join-Path $RepoRoot "out\live\$Slug-live.pgn"
$Python = (Get-Command python -ErrorAction Stop).Source

Stop-ViewerOnPort -TargetPort $Port
New-Item -ItemType File -Path $LivePgn -Force | Out-Null
Start-Process -FilePath $Python -ArgumentList @(
  $ViewerScript, '--pgn', $LivePgn, '--port', "$Port", '--stats-dir', (Join-Path $RepoRoot 'out')
) -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null

$LiveUrl = "$BaseUrl/#$([uri]::EscapeDataString("$Slug--live-game-1"))"
Write-Host "Composer train→test loop (local only, no API)"
Write-Host "Live: $LiveUrl"
Write-Host "Log: out\composer-training\loop-log.jsonl"
if (-not $NoOpen) { Start-Process $LiveUrl }

$Args = @(
  $LoopScript,
  '--target-depth', "$TargetDepth",
  '--games', "$Games",
  '--pass-score', "$PassScore",
  '--composer-ms', "$ComposerMs",
  '--min-chars', "$MinChars",
  '--live-pgn', $LivePgn
)
if ($Reset) { $Args += '--reset' }

& $Python @Args
exit $LASTEXITCODE
