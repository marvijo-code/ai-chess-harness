<#
.SYNOPSIS
  Record one live tournament round on the OVH VPS (screen + commentary speech + move clicks), then collect it.

.DESCRIPTION
  Arm (default):  starts the hidden reverse tunnel (laptop -Port -> VPS -RemotePort), deploys the recorder
                  scripts to ~/acl-chess-round-rec/bin on the VPS and arms rec.py for -Round in tmux.
                  Arm BEFORE the round starts; the recorder waits (up to 12 h) and starts at the round start.
  -Status:        shows the recorder state for that round.
  -Collect:       after the round ended, mixes the audio, muxes the lossless master, verifies it on the VPS,
                  downloads it plus the QC files, copies it byte-equal to C:\temp (SHA-256 compared).
  -WaitChange:    the round already exists but the runner is stopped (paused tournament): the take starts
                  the moment the round's games change (the resumed runner restarts them from move 1).
  -StopTunnel:    stops the tunnel PID this launcher started (nothing else). Can be combined with -Collect.
  No ffmpeg runs on the laptop. The tunnel window is hidden (run-hidden-cmd.vbs).

.EXAMPLE
  pwsh tools\round_recorder\record-round.ps1 -Round 3
  pwsh tools\round_recorder\record-round.ps1 -Round 3 -Status
  pwsh tools\round_recorder\record-round.ps1 -Round 3 -Collect
  pwsh tools\round_recorder\record-round.ps1 -Round 10 -Slug llm-swiss-20261006-130404 -Collect -Format mp4 -StopTunnel
#>
param(
  [Parameter(Mandatory = $true)][int]$Round,
  [string]$Slug = "",
  [int]$Port = 8770,
  [int]$RemotePort = 18770,
  [int]$Display = 97,
  [double]$MaxMinutes = 75,
  [ValidateSet("mkv", "mp4")][string]$Format = "mkv",
  [string]$RunDir = "",
  [string]$Vps = $env:ROUND_RECORDER_VPS,  # user@host of the recording VPS (kept out of this public repo)
  [string]$RemoteBase = "acl-chess-round-rec",
  [string]$Session = "aclrec",
  [string]$OutDir = "C:\dev\hyperframes-student-kit\video-projects\acl-ai-chess-swiss-2026-10-05\recordings",
  [string]$CopyDir = "C:\temp",
  [string]$CopyPrefix = "acl-ai-chess-",
  [switch]$Collect,
  [switch]$Status,
  [switch]$StopTunnel,
  [switch]$AllowLate,
  [switch]$WaitChange
)
if (-not $Vps) { throw "Pass -Vps user@host or set ROUND_RECORDER_VPS (the recording VPS is not stored in the repo)." }
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $here "..\..")).Path
$state = Join-Path $repo "out\round-recorder"
New-Item -ItemType Directory -Force $state | Out-Null
$pidFile = Join-Path $state "tunnel-$RemotePort.pid"
$sshOpts = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30")

function Invoke-Vps([string]$cmd) {
  $out = & ssh @sshOpts $Vps $cmd 2>&1
  return @{ Code = $LASTEXITCODE; Text = ($out -join "`n") }
}

function Get-TunnelProcess {
  $spec = "$($RemotePort):127.0.0.1:$($Port)"
  if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile -Raw).Trim()
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    if ($p -and $p.Name -eq "ssh.exe" -and $p.CommandLine -like "*$spec*") { return $p }
  }
  # A tunnel with the same forward started earlier by hand: reuse it (but never stop it with -StopTunnel).
  return Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like "*-R $spec*" } | Select-Object -First 1
}

function Start-Tunnel {
  $p = Get-TunnelProcess
  if (-not $p) {
    $cmdFile = Join-Path $state "tunnel-$RemotePort.cmd"
    $logFile = Join-Path $state "tunnel-$RemotePort.log"
    Set-Content -Encoding ascii $cmdFile "@echo off`r`nssh -N -o BatchMode=yes -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R $($RemotePort):127.0.0.1:$($Port) $Vps > `"$logFile`" 2>&1`r`n"
    Start-Process wscript.exe -ArgumentList "//B", "//Nologo", (Join-Path $repo "tools\run-hidden-cmd.vbs"), "`"$cmdFile`""
    for ($i = 0; $i -lt 20 -and -not $p; $i++) { Start-Sleep -Milliseconds 500; $p = Get-TunnelProcess }
    if (-not $p) { throw "tunnel did not start; see $logFile" }
    Set-Content $pidFile $p.ProcessId
    Start-Sleep 2
    Write-Host "tunnel started, PID $($p.ProcessId) (pid file $pidFile)"
  } else {
    Write-Host "tunnel already up, PID $($p.ProcessId)"
  }
  $r = Invoke-Vps "curl -s localhost:$RemotePort/api/viewer-version"
  if ($r.Text -notmatch '"version"') { throw "viewer not reachable through the tunnel: $($r.Text)" }
  Write-Host "VPS sees the viewer: $($r.Text)"
}

function Stop-OwnTunnel {
  if (-not (Test-Path $pidFile)) { Write-Host "no tunnel pid file; nothing stopped"; return }
  $id = [int](Get-Content $pidFile -Raw).Trim()
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
  if ($p -and $p.Name -eq "ssh.exe" -and $p.CommandLine -like "*$($RemotePort):127.0.0.1:$($Port)*") {
    Stop-Process -Id $id -Confirm:$false
    Write-Host "tunnel PID $id stopped"
  } else { Write-Host "tunnel PID $id is not running" }
  Remove-Item $pidFile -Confirm:$false
}

function Deploy-Scripts {
  $files = "rec.py", "mix.py", "click_sound.py", "arm.sh", "collect.sh", "status.sh" | ForEach-Object { Join-Path $here $_ }
  $r = Invoke-Vps "mkdir -p ~/$RemoteBase/bin ~/$RemoteBase/runs"
  if ($r.Code -ne 0) { throw "VPS mkdir failed: $($r.Text)" }
  & scp -q @sshOpts @files "$($Vps):$RemoteBase/bin/"
  if ($LASTEXITCODE -ne 0) { throw "scp of the scripts failed" }
  # Channel music bed for the mix (no silent stretches); uploaded once.
  $bed = "C:\dev\ai-tools\assets\audio\music\marvijo-channel-bed-cinematic-ambient.mp3"
  if (Test-Path -LiteralPath $bed) {
    $have = Invoke-Vps "test -f ~/$RemoteBase/bed-cinematic-ambient.mp3 && echo yes || echo no"
    if ("$have" -notmatch "yes") { & scp -q @sshOpts $bed "$($Vps):$RemoteBase/bed-cinematic-ambient.mp3" }
  }
  $r = Invoke-Vps "cd ~/$RemoteBase/bin && sed -i 's/\r`$//' *.sh *.py && chmod +x *.sh"
  if ($r.Code -ne 0) { throw "VPS script prep failed: $($r.Text)" }
}

if (-not $Slug) {
  try { $Slug = (Invoke-RestMethod "http://127.0.0.1:$Port/api/tournament" -TimeoutSec 10).id } catch { }
  if (-not $Slug) { throw "could not read the tournament id from http://127.0.0.1:$Port/api/tournament; pass -Slug" }
}
if (-not $RunDir) { $RunDir = "runs/$Slug-round$Round" }
Write-Host "tournament $Slug, round $Round, VPS run dir ~/$RemoteBase/$RunDir"

if ($Status) {
  Deploy-Scripts
  $r = Invoke-Vps "bash ~/$RemoteBase/bin/status.sh $RunDir $Session"
  Write-Host $r.Text
  if ($StopTunnel) { Stop-OwnTunnel }
  exit 0
}

if (-not $Collect) {
  Start-Tunnel
  Deploy-Scripts
  $late = if ($WaitChange) { 2 } elseif ($AllowLate) { 1 } else { 0 }
  $r = Invoke-Vps "bash ~/$RemoteBase/bin/arm.sh $RunDir $Round $Slug $RemotePort $Display $MaxMinutes $late $Session"
  Write-Host $r.Text
  if ($r.Code -ne 0) { throw "arming failed (exit $($r.Code))" }
  Write-Host "Armed. Leave the tunnel up. After the round: record-round.ps1 -Round $Round -Collect"
  exit 0
}

# ---- collect --------------------------------------------------------------------------------
Start-Tunnel
Deploy-Scripts
New-Item -ItemType Directory -Force $OutDir | Out-Null
$n = 1
while (Test-Path (Join-Path $OutDir ("round{0}-live-DRAFT{1:000}.{2}" -f $Round, $n, $Format))) { $n++ }
$name = "round{0}-live-DRAFT{1:000}.{2}" -f $Round, $n, $Format
Write-Host "mixing and muxing $name on the VPS (format $Format)..."
$r = Invoke-Vps "bash ~/$RemoteBase/bin/collect.sh $RunDir $name $Format $RemotePort $Session"
Write-Host $r.Text
if ($r.Code -ne 0) { throw "collect failed on the VPS (exit $($r.Code))" }
$remoteSha = ([regex]::Match($r.Text, '(?m)^([0-9a-f]{64})\s')).Groups[1].Value
$dest = Join-Path $OutDir $name
& scp -q @sshOpts "$($Vps):$RemoteBase/$RunDir/$name" $dest
if ($LASTEXITCODE -ne 0) { throw "download failed" }
$qc = Join-Path $OutDir ($name -replace '\.\w+$', '-qc')
New-Item -ItemType Directory -Force $qc | Out-Null
& scp -q -r @sshOpts "$($Vps):$RemoteBase/$RunDir/grabs" "$($Vps):$RemoteBase/$RunDir/verify.txt" "$($Vps):$RemoteBase/$RunDir/mix/plan.json" $qc
$localSha = (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower()
if ($remoteSha -and $localSha -ne $remoteSha) { throw "SHA-256 mismatch after download: VPS $remoteSha, laptop $localSha" }
New-Item -ItemType Directory -Force $CopyDir | Out-Null
$copy = Join-Path $CopyDir ($CopyPrefix + $name)
if (Test-Path $copy) { Remove-Item $copy -Confirm:$false }
$need = (Get-Item $dest).Length + 200MB
$drive = Get-PSDrive -Name ($CopyDir.Substring(0, 1))
$sameVolume = $CopyDir.Substring(0, 1) -eq $dest.Substring(0, 1)
if ($drive.Free -lt $need -and $sameVolume) {
  # Not enough space for a second copy on this drive: an NTFS hard link gives the same bytes for free.
  New-Item -ItemType HardLink -Path $copy -Target $dest | Out-Null
  Write-Host "low disk space ($([math]::Round($drive.Free / 1MB)) MB free): $copy is a hard link to the master"
} else {
  Copy-Item $dest $copy -Force
}
$copySha = (Get-FileHash -Algorithm SHA256 $copy).Hash.ToLower()
if ($copySha -ne $localSha) { throw "SHA-256 mismatch for the C:\temp copy" }
Write-Host ""
Write-Host "master : $dest"
Write-Host "copy   : $copy"
Write-Host "qc     : $qc"
Write-Host ("size   : {0:N1} MB" -f ((Get-Item $dest).Length / 1MB))
Write-Host "sha256 : $localSha (VPS, laptop and C:\temp copy match)"
if ($StopTunnel) { Stop-OwnTunnel }
