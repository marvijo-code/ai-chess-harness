<#
.SYNOPSIS
  Deploy the AI-chess relay to the VPS: copy the code, install the ingest token, run it in tmux.

.DESCRIPTION
  Everything lives in ~/acl-chess-relay on the VPS (own directory, no sudo, no nginx changes).
  The relay runs inside tmux session "aclrelay" in a restart loop (run-relay.sh).
  The VPS login comes from -Vps, else the user environment variable ROUND_RECORDER_VPS, else the
  gitignored out\aichess-push\vps.txt saved by the last run that passed -Vps.
  The ingest token is read from ~\.aichess-ingest-token (created when missing) and piped over
  ssh stdin to ~/acl-chess-relay/ingest.token (chmod 600). It is never printed: only its length
  and a SHA-256 prefix are shown, for both copies.

  -Restart   kill and recreate ONLY the tmux session "aclrelay" (picks up new code)
  -Status    show the session and the public /healthz, change nothing

.EXAMPLE
  .\tools\aichess_relay_vps\deploy.ps1
.EXAMPLE
  .\tools\aichess_relay_vps\deploy.ps1 -Restart
#>
[CmdletBinding()]
param(
  [string]$Vps,
  [switch]$Restart,
  [switch]$Status,
  [int]$PublicPort = 8780,
  [string]$TokenFile = (Join-Path $HOME '.aichess-ingest-token')
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $PSCommandPath
$RepoRoot = Split-Path -Parent (Split-Path -Parent $Here)
# user@host is kept out of this public repo: -Vps, else ROUND_RECORDER_VPS, else the gitignored
# out\aichess-push\vps.txt (shared with run-aichess-push.ps1).
$StateDir = Join-Path $RepoRoot 'out\aichess-push'
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
$VpsFile = Join-Path $StateDir 'vps.txt'
if ($Vps) { Set-Content -LiteralPath $VpsFile -Value $Vps -Encoding ASCII }
if (-not $Vps) { $Vps = [Environment]::GetEnvironmentVariable('ROUND_RECORDER_VPS', 'User') }
if (-not $Vps -and (Test-Path -LiteralPath $VpsFile)) { $Vps = (Get-Content -LiteralPath $VpsFile -Raw).Trim() }
if (-not $Vps) { throw 'Pass -Vps user@host or set ROUND_RECORDER_VPS (the VPS is not stored in the repo).' }
$HostOnly = ($Vps -split '@')[-1]
$Ssh = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20')

function Invoke-Vps([string]$Command) {
  $out = & ssh @Ssh $Vps $Command
  if ($LASTEXITCODE -ne 0) { throw "ssh command failed ($LASTEXITCODE)" }
  return $out
}

function Show-Health {
  Invoke-Vps 'tmux has-session -t aclrelay 2>/dev/null && echo "tmux aclrelay: running" || echo "tmux aclrelay: MISSING"; tail -n 3 ~/acl-chess-relay/relay.log 2>/dev/null' | Write-Host
  try {
    $h = Invoke-RestMethod -Uri "http://$($HostOnly):$PublicPort/healthz" -TimeoutSec 10
    Write-Host "public healthz (from this laptop): $($h | ConvertTo-Json -Compress)"
  } catch { Write-Host "public healthz NOT reachable from this laptop: $($_.Exception.Message)" }
}

if ($Status) { Show-Health; return }

# 1. Token: create locally when missing, then install the same value on the VPS through stdin.
if (-not (Test-Path -LiteralPath $TokenFile)) {
  $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
  [IO.File]::WriteAllText($TokenFile, (-join ($bytes | ForEach-Object { $_.ToString('x2') })))
  Write-Host "created ingest token file $TokenFile"
}
$token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
$sha = [System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($token))
$localPrefix = (-join ($sha | ForEach-Object { $_.ToString('x2') })).Substring(0, 12)
Invoke-Vps 'mkdir -p ~/acl-chess-relay/data && chmod 700 ~/acl-chess-relay' | Out-Null
$token | & ssh @Ssh $Vps 'umask 077; cat > ~/acl-chess-relay/ingest.token.new && mv ~/acl-chess-relay/ingest.token.new ~/acl-chess-relay/ingest.token && chmod 600 ~/acl-chess-relay/ingest.token'
if ($LASTEXITCODE -ne 0) { throw 'token install failed' }
$remote = Invoke-Vps "tr -d '\r\n' < ~/acl-chess-relay/ingest.token | sha256sum | cut -c1-12; tr -d '\r\n' < ~/acl-chess-relay/ingest.token | wc -c; stat -c %a ~/acl-chess-relay/ingest.token"
Write-Host "token: local length $($token.Length) sha256 $localPrefix... | VPS sha256 $($remote[0])... length $($remote[1].Trim()) mode $($remote[2])"
if ($remote[0].Trim() -ne $localPrefix) { throw 'token on the VPS does not match the local token' }

# 2. Code: scp, then strip CR line endings from the shell script.
& scp @Ssh -q (Join-Path $RepoRoot 'tools\aichess_relay.py') (Join-Path $Here 'run-relay.sh') "$($Vps):acl-chess-relay/"
if ($LASTEXITCODE -ne 0) { throw 'scp failed' }
Invoke-Vps "sed -i 's/\r$//' ~/acl-chess-relay/run-relay.sh && python3 -m py_compile ~/acl-chess-relay/aichess_relay.py && echo code ok" | Write-Host

# 3. Run: only the tmux session "aclrelay" is ever touched.
if ($Restart) { Invoke-Vps 'tmux kill-session -t aclrelay 2>/dev/null; true' | Out-Null; Start-Sleep 1 }
Invoke-Vps 'tmux has-session -t aclrelay 2>/dev/null && echo "aclrelay already running (use -Restart for new code)" || (tmux new-session -d -s aclrelay "sh $HOME/acl-chess-relay/run-relay.sh" && echo "aclrelay started")' | Write-Host
Start-Sleep 2
Show-Health
