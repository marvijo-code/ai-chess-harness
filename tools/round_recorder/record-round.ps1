<#
.SYNOPSIS
  Record live tournament rounds on the OVH VPS (screen + commentary speech + move clicks), then collect them.

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

  Series (the rest of the tournament, one DRAFT per round, board tours time-lapsed):
  -Series -FromRound N [-WaitChange]: tunnel, deploy, arm rec.py series (tmux aclrec) and mixer.sh (tmux aclmix).
  -SeriesStatus:  per round: recorded?, mixed?, raw and DRAFT sizes, SHA-256, last rec.log lines, VPS free disk.
  -CollectNew:    downloads every round<R>-live-DRAFT001.mkv mixed with rc 0 and not yet downloaded (SHA-256
                  checked) into -OutDir, plus its QC files, and hard-links C:\temp\acl-ai-chess-round<R>-live-DRAFT001.mkv.
  -Prune:         deletes on the VPS ONLY raw-r<R>.mkv of rounds whose DRAFT was downloaded and SHA-verified.

.EXAMPLE
  pwsh tools\round_recorder\record-round.ps1 -Series -FromRound 5 -WaitChange
  pwsh tools\round_recorder\record-round.ps1 -SeriesStatus
  pwsh tools\round_recorder\record-round.ps1 -CollectNew -Prune
  pwsh tools\round_recorder\record-round.ps1 -Round 3
  pwsh tools\round_recorder\record-round.ps1 -Round 3 -Status
  pwsh tools\round_recorder\record-round.ps1 -Round 3 -Collect
  pwsh tools\round_recorder\record-round.ps1 -Round 10 -Slug llm-swiss-20261006-130404 -Collect -Format mp4 -StopTunnel
#>
param(
  [int]$Round = 0,
  [switch]$Series,
  [int]$FromRound = 0,
  [switch]$SeriesStatus,
  [switch]$CollectNew,
  [switch]$Prune,
  [double]$SeriesMaxMinutes = 360,
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
if (-not $Vps) { $Vps = [Environment]::GetEnvironmentVariable('ROUND_RECORDER_VPS', 'User') }
if (-not $Vps) { throw "Pass -Vps user@host or set ROUND_RECORDER_VPS (the recording VPS is not stored in the repo)." }
$seriesMode = $Series -or $SeriesStatus -or $CollectNew -or $Prune
if (-not $seriesMode -and $Round -le 0) { throw "Pass -Round N (one round) or -Series -FromRound N / -SeriesStatus / -CollectNew / -Prune." }
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
  $files = "rec.py", "mix.py", "timeline.py", "click_sound.py", "arm.sh", "collect.sh", "status.sh",
    "series.sh", "mixer.sh", "series-status.sh" | ForEach-Object { Join-Path $here $_ }
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
if ($seriesMode) {
  # ---- series: every remaining round, one take per round, mixed per round on the VPS ----------------
  $seriesFile = Join-Path $state "series-$Slug.json"
  if (-not $RunDir) {
    if ($Series) {
      if ($FromRound -le 0) { throw "-Series needs -FromRound N" }
      $RunDir = "runs/$Slug-series-from$FromRound"
    } elseif (Test-Path $seriesFile) {
      $RunDir = (Get-Content $seriesFile -Raw | ConvertFrom-Json).run_dir
    } elseif ($FromRound -gt 0) {
      $RunDir = "runs/$Slug-series-from$FromRound"
    } else { throw "no armed series recorded in $seriesFile; pass -FromRound N or -RunDir" }
  }
  Write-Host "tournament $Slug, series run dir ~/$RemoteBase/$RunDir"
  $ledgerFile = Join-Path $state ("series-{0}-downloads.json" -f ($RunDir -replace '[^\w.-]', '_'))
  $ledger = @{}
  if (Test-Path $ledgerFile) { (Get-Content $ledgerFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $ledger[$_.Name] = $_.Value } }

  function Get-SeriesRounds {
    $have = Invoke-Vps "test -f ~/$RemoteBase/bin/series-status.sh && echo yes || echo no"
    if ("$($have.Text)" -notmatch "yes") {
      # Only the read-only status script is uploaded here (never mixer.sh, which may be running).
      Invoke-Vps "mkdir -p ~/$RemoteBase/bin" | Out-Null
      & scp -q @sshOpts (Join-Path $here "series-status.sh") "$($Vps):$RemoteBase/bin/"
      Invoke-Vps "sed -i 's/\r`$//' ~/$RemoteBase/bin/series-status.sh" | Out-Null
    }
    $r = Invoke-Vps "bash ~/$RemoteBase/bin/series-status.sh $RunDir"
    $rows = @()
    foreach ($line in ($r.Text -split "`n")) {
      $m = [regex]::Match($line, '^ROUND key=(\S+) recorded=(\S+) mixed=(\S+) raw=(\S+) draft=(\S+) take_s=(\S+) reason=(\S+) sha256=(\S+)(?: name=(\S+))?')
      if ($m.Success) {
        $rows += [pscustomobject]@{ Key = $m.Groups[1].Value; Recorded = $m.Groups[2].Value; Mixed = $m.Groups[3].Value
          Raw = $m.Groups[4].Value; Draft = $m.Groups[5].Value; Sha = $m.Groups[8].Value
          Name = $(if ($m.Groups[9].Success) { $m.Groups[9].Value } else { "round$($m.Groups[1].Value)-live-DRAFT001.mkv" }) }
      }
    }
    return @{ Text = $r.Text; Rows = $rows }
  }

  if ($Series) {
    Start-Tunnel
    Deploy-Scripts
    $wc = if ($WaitChange) { 1 } else { 0 }
    $r = Invoke-Vps "bash ~/$RemoteBase/bin/series.sh $RunDir $FromRound $Slug $RemotePort $Display $SeriesMaxMinutes $wc"
    Write-Host $r.Text
    if ($r.Code -ne 0) { throw "arming the series failed (exit $($r.Code))" }
    @{ run_dir = $RunDir; from_round = $FromRound; slug = $Slug; armed = (Get-Date).ToString("s") } | ConvertTo-Json | Set-Content $seriesFile
    Write-Host "Series armed. Leave the tunnel up. Check: record-round.ps1 -SeriesStatus ; fetch DRAFTs: record-round.ps1 -CollectNew"
    exit 0
  }

  if ($SeriesStatus) {
    # No redeploy here: mixer.sh is running on the VPS and must not be overwritten mid-run.
    $s = Get-SeriesRounds
    Write-Host $s.Text
    foreach ($row in $s.Rows) {
      $dl = if ($ledger.ContainsKey($row.Key)) { "downloaded" } else { "not downloaded" }
      Write-Host ("round {0}: recorded {1}, mixed {2}, raw {3}, draft {4}, {5}" -f $row.Key, $row.Recorded, $row.Mixed, $row.Raw, $row.Draft, $dl)
    }
    if ($StopTunnel) { Stop-OwnTunnel }
    exit 0
  }

  if ($CollectNew) {
    New-Item -ItemType Directory -Force $OutDir, $CopyDir | Out-Null
    $s = Get-SeriesRounds
    $got = 0
    foreach ($row in $s.Rows) {
      if ($row.Mixed -ne "rc=0") { continue }
      if ($row.Key -notmatch '^[0-9]+(p[0-9]+)?$') { Write-Host "skipping odd key $($row.Key)"; continue }
      $remoteName = $row.Name
      if ($remoteName -notmatch '^round[0-9]+(p[0-9]+)?-live-DRAFT[0-9]{3}\.mkv$') { Write-Host "skipping odd draft name $remoteName"; continue }
      $sha = $row.Sha
      if ($sha -notmatch '^[0-9a-f]{64}$') {
        $h = Invoke-Vps "sha256sum ~/$RemoteBase/$RunDir/$remoteName | cut -d' ' -f1"
        $sha = $h.Text.Trim()
        if ($sha -notmatch '^[0-9a-f]{64}$') { Write-Host "round $($row.Key): no SHA-256 on the VPS ($sha); skipped"; continue }
      }
      if ($ledger.ContainsKey($row.Key) -and $ledger[$row.Key].sha -eq $sha -and (Test-Path $ledger[$row.Key].dest)) { continue }
      $n = [int]([regex]::Match($remoteName, 'DRAFT([0-9]{3})').Groups[1].Value)
      $dest = Join-Path $OutDir ("round{0}-live-DRAFT{1:000}.mkv" -f $row.Key, $n)
      while (Test-Path $dest) {
        if ((Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower() -eq $sha) { break }
        $n++
        $dest = Join-Path $OutDir ("round{0}-live-DRAFT{1:000}.mkv" -f $row.Key, $n)
      }
      if (-not (Test-Path $dest)) {
        Write-Host "round $($row.Key): downloading $remoteName ($($row.Draft)) -> $dest"
        $part = "$dest.partial"
        & scp -q @sshOpts "$($Vps):$RemoteBase/$RunDir/$remoteName" $part
        if ($LASTEXITCODE -ne 0) { Remove-Item $part -ErrorAction SilentlyContinue -Confirm:$false; throw "download of $remoteName failed" }
        $localSha = (Get-FileHash -Algorithm SHA256 $part).Hash.ToLower()
        if ($localSha -ne $sha) { Remove-Item $part -Confirm:$false; throw "SHA-256 mismatch for ${remoteName}: VPS $sha, laptop $localSha" }
        Move-Item $part $dest
      }
      $qc = $dest -replace '\.mkv$', '-qc'
      New-Item -ItemType Directory -Force $qc | Out-Null
      & scp -q -r @sshOpts "$($Vps):$RemoteBase/$RunDir/grabs-r$($row.Key)" "$($Vps):$RemoteBase/$RunDir/round$($row.Key)-verify.txt" "$($Vps):$RemoteBase/$RunDir/mix-r$($row.Key)/plan.json" $qc
      $copy = Join-Path $CopyDir ($CopyPrefix + (Split-Path $dest -Leaf))
      if (Test-Path $copy) { Remove-Item $copy -Confirm:$false }
      $linked = $false
      if ($CopyDir.Substring(0, 1) -eq $dest.Substring(0, 1)) {
        try { New-Item -ItemType HardLink -Path $copy -Target $dest -ErrorAction Stop | Out-Null; $linked = $true }
        catch { Write-Host "hard link failed ($($_.Exception.Message)); copying instead" }
      }
      if (-not $linked) { Copy-Item $dest $copy -Force }
      $copySha = (Get-FileHash -Algorithm SHA256 $copy).Hash.ToLower()
      if ($copySha -ne $sha) { throw "SHA-256 mismatch for $copy" }
      $ledger[$row.Key] = [pscustomobject]@{ sha = $sha; dest = $dest; copy = $copy; at = (Get-Date).ToString("s") }
      $ledger | ConvertTo-Json | Set-Content $ledgerFile
      Write-Host ("round {0}: {1} ({2:N1} MB), copy {3}, sha256 {4} (VPS, laptop and copy match)" -f $row.Key, $dest, ((Get-Item $dest).Length / 1MB), $copy, $sha)
      $got++
    }
    Write-Host "$got new DRAFT(s) collected."
  }

  if ($Prune) {
    # Deletes ONLY raw-r<key>.mkv of rounds whose DRAFT is downloaded and SHA-256 verified again now.
    $s = Get-SeriesRounds
    foreach ($row in $s.Rows) {
      if (-not $ledger.ContainsKey($row.Key)) { continue }
      if ($row.Key -notmatch '^[0-9]+(p[0-9]+)?$' -or $row.Raw -eq "-") { continue }
      $entry = $ledger[$row.Key]
      if (-not (Test-Path $entry.dest)) { Write-Host "round $($row.Key): local DRAFT missing; raw kept"; continue }
      $localSha = (Get-FileHash -Algorithm SHA256 $entry.dest).Hash.ToLower()
      if ($localSha -ne $entry.sha -or $localSha -ne $row.Sha) { Write-Host "round $($row.Key): SHA-256 differs (local $localSha, VPS $($row.Sha)); raw kept"; continue }
      $r = Invoke-Vps "rm -f -- ~/$RemoteBase/$RunDir/raw-r$($row.Key).mkv && echo PRUNED"
      Write-Host "round $($row.Key): raw-r$($row.Key).mkv $($r.Text.Trim())"
    }
    $r = Invoke-Vps "df -h ~ | tail -1"
    Write-Host "VPS disk: $($r.Text.Trim())"
  }
  if ($StopTunnel) { Stop-OwnTunnel }
  exit 0
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
