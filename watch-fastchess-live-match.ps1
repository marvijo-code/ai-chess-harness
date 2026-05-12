param(
    [int]$Games = 10,
    [int]$Concurrency = 1,
    [string]$Model = "gpt-5.5",
    [string]$Effort = "low",
    [string]$TimeControl = "300+0",
    [int]$MaxMoves = 0,
    [string]$FastChessVersion = "latest",
    [int]$AnalysisMovetimeMs = 250,
    [int]$AnalysisMultipv = 3,
    [string]$PgnPath = "",
    [switch]$ForceInstall,
    [switch]$NoAnalysis,
    [switch]$NoBrowser,
    [switch]$AttachLatest,
    [switch]$StartNewRun,
    [switch]$StopViewerWhenDone,
    [switch]$NoLearnerAutoLearn,
    [switch]$SkipModelPreflight,
    [switch]$NoRepeat
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-RepoRoot {
    $candidate = Resolve-Path $PSScriptRoot
    if (Test-Path (Join-Path $candidate "tools\live_pgn_viewer.py")) {
        return $candidate
    }

    $parent = Resolve-Path (Join-Path $PSScriptRoot "..")
    if (Test-Path (Join-Path $parent "tools\live_pgn_viewer.py")) {
        return $parent
    }

    throw "Could not resolve repo root from script path: $PSScriptRoot"
}

function Test-PortAvailable {
    param([int]$CandidatePort)

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Parse("127.0.0.1"),
        $CandidatePort
    )
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        $listener.Stop()
    }
}

function ConvertTo-ProcessArguments {
    param([string[]]$Arguments)

    return ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    })
}

function Get-LiveViewerProcessOnPort {
    param([int]$ViewerPort)

    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -match "live_pgn_viewer\.py" -and
            $_.CommandLine -match "--port\s+$ViewerPort(?:\s|$)"
        }
}

function ConvertTo-AbsolutePath {
    param(
        [string]$PathText,
        [string]$BasePath
    )

    if ([System.IO.Path]::IsPathRooted($PathText)) {
        return $PathText
    }
    return Join-Path $BasePath $PathText
}

function Get-RunningFastChessPgnPath {
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -ieq "fastchess.exe" -and $_.CommandLine -match "-pgnout" } |
        ForEach-Object {
            $commandLine = $_.CommandLine
            if ($commandLine -match '(?i)(?:^|\s)file=(".*?\.pgn"|[^\s"]+\.pgn)') {
                [PSCustomObject]@{
                    ProcessId = $_.ProcessId
                    PgnPath = ($Matches[1] -replace '^"', '' -replace '"$', '')
                }
            }
        } |
        Select-Object -First 1
}

function Get-LatestFastChessPgnPath {
    param([string]$ResultsRoot)

    Get-ChildItem -LiteralPath $ResultsRoot -Filter "*.pgn" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

if ($Games -lt 1) {
    throw "-Games must be at least 1."
}

$repoRoot = Resolve-RepoRoot
$resultsRoot = Join-Path $repoRoot "out\fastchess"
$viewerScript = Join-Path $repoRoot "tools\live_pgn_viewer.py"
$mirrorScript = Join-Path $repoRoot "tools\mirror_fastchess_live_pgn.py"
$runnerScript = Join-Path $repoRoot "install-and-run-fastchess-codex.ps1"
$autolearnScript = Join-Path $repoRoot "tools\update_learner_knowledgebase.py"
$preflightScript = Join-Path $repoRoot "tools\check_codex_model_available.py"

if (-not (Test-Path $viewerScript)) {
    throw "Live PGN viewer script was not found: $viewerScript"
}
if (-not (Test-Path $mirrorScript)) {
    throw "FastChess live mirror script was not found: $mirrorScript"
}
if (-not (Test-Path $runnerScript)) {
    throw "FastChess runner script was not found: $runnerScript"
}
if (-not (Test-Path $autolearnScript)) {
    throw "Learner autolearn script was not found: $autolearnScript"
}
if (-not (Test-Path $preflightScript)) {
    throw "Codex model preflight script was not found: $preflightScript"
}

New-Item -ItemType Directory -Force -Path $resultsRoot | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runName = "codex-vs-codex-learner-live"
$pgnPath = $null
$runFastChess = $false

if ($PgnPath -ne "") {
    $pgnPath = ConvertTo-AbsolutePath -PathText $PgnPath -BasePath $repoRoot
    Write-Host "Watching explicit PGN path: $pgnPath"
} elseif ($AttachLatest) {
    $pgnPath = Get-LatestFastChessPgnPath -ResultsRoot $resultsRoot
    if (-not $pgnPath) {
        throw "No FastChess PGN files were found under $resultsRoot."
    }
    Write-Host "Watching latest FastChess PGN: $pgnPath"
} elseif (-not $StartNewRun) {
    $runningFastChess = Get-RunningFastChessPgnPath
    if ($runningFastChess) {
        $pgnPath = $runningFastChess.PgnPath
        Write-Host "Attached to FastChess process $($runningFastChess.ProcessId)."
        Write-Host "Watching active FastChess PGN: $pgnPath"
    }
}

if (-not $pgnPath) {
    $pgnPath = Join-Path $resultsRoot "$runName-$stamp.pgn"
    $runFastChess = $true
}

$modelAlreadyChecked = $false
if ($runFastChess -and (-not $SkipModelPreflight)) {
    Write-Host "Checking Codex model availability before starting viewer/run: $Model"
    & python $preflightScript --model $Model --effort $Effort
    if ($LASTEXITCODE -ne 0) {
        throw "Codex model preflight failed for $Model. Choose another -Model or wait for the model limit reset before starting FastChess."
    }
    $modelAlreadyChecked = $true
}

$viewerPort = 8766
if (-not (Test-PortAvailable -CandidatePort $viewerPort)) {
    $existingViewers = @(Get-LiveViewerProcessOnPort -ViewerPort $viewerPort)
    if ($existingViewers.Count -gt 0) {
        $ids = $existingViewers | Select-Object -ExpandProperty ProcessId
        Write-Host "Restarting existing live viewer on dedicated port ${viewerPort}: $($ids -join ', ')"
        $ids | ForEach-Object { Stop-Process -Id $_ -Force }
        Start-Sleep -Milliseconds 500
    }
}
if (-not (Test-PortAvailable -CandidatePort $viewerPort)) {
    throw "Dedicated live viewer port $viewerPort is already in use by another process. Stop that process before starting the FastChess live viewer."
}
$viewerUrl = "http://127.0.0.1:$viewerPort/"
$viewerOut = Join-Path $resultsRoot "$runName-$stamp-viewer.out.log"
$viewerErr = Join-Path $resultsRoot "$runName-$stamp-viewer.err.log"
$launchOut = Join-Path $resultsRoot "$runName-$stamp-launch.out.log"
$autolearnOut = Join-Path $resultsRoot "$runName-$stamp-autolearn.out.log"
$autolearnErr = Join-Path $resultsRoot "$runName-$stamp-autolearn.err.log"
$liveRoot = Join-Path $repoRoot "out\live"
$engineLogRoot = Join-Path $repoRoot "out\codex-chess-logs"
$mirrorProcess = $null
$viewerPgnPath = $pgnPath
$mirrorOut = Join-Path $resultsRoot "$runName-$stamp-mirror.out.log"
$mirrorErr = Join-Path $resultsRoot "$runName-$stamp-mirror.err.log"

New-Item -ItemType Directory -Force -Path $liveRoot | Out-Null

$candidateLaunchOut = $launchOut
if (-not $runFastChess) {
    $candidateLaunchOut = [System.IO.Path]::ChangeExtension($pgnPath, $null) + "-launch.out.log"
}

if ($runFastChess -or (Test-Path $candidateLaunchOut)) {
    $pgnLeaf = [System.IO.Path]::GetFileNameWithoutExtension($pgnPath)
    $viewerPgnPath = Join-Path $liveRoot "$pgnLeaf-live.pgn"
    $mirrorArgs = @(
        $mirrorScript,
        "--fastchess-stdout", $candidateLaunchOut,
        "--engine-log-dir", $engineLogRoot,
        "--output", $viewerPgnPath,
        "--interval", "1"
    )
    $mirrorProcess = Start-Process `
        -FilePath "python" `
        -ArgumentList (ConvertTo-ProcessArguments -Arguments $mirrorArgs) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $mirrorOut `
        -RedirectStandardError $mirrorErr `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "FastChess live mirror is running as process $($mirrorProcess.Id)."
    Write-Host "Viewer mirror PGN: $viewerPgnPath"
}

$viewerArgs = @(
    $viewerScript,
    "--pgn", $viewerPgnPath,
    "--host", "127.0.0.1",
    "--port", "$viewerPort",
    "--stats-dir", (Join-Path $repoRoot "out"),
    "--analysis-movetime-ms", "$AnalysisMovetimeMs",
    "--analysis-multipv", "$AnalysisMultipv"
)
if ($NoAnalysis) {
    $viewerArgs += "--no-analysis"
}

Write-Host "Starting local live viewer: $viewerUrl"
Write-Host "FastChess PGN output: $pgnPath"
Write-Host "Viewer PGN: $viewerPgnPath"
Write-Host "FastChess PGN output updates as FastChess writes it; use play_codex_vs_stockfish.py for ply-by-ply live PGN."

$viewerProcess = Start-Process `
    -FilePath "python" `
    -ArgumentList (ConvertTo-ProcessArguments -Arguments $viewerArgs) `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $viewerOut `
    -RedirectStandardError $viewerErr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Milliseconds 700

if ($viewerProcess.HasExited) {
    $errorText = if (Test-Path $viewerErr) { Get-Content -LiteralPath $viewerErr -Raw } else { "" }
    throw "Live PGN viewer exited early with code $($viewerProcess.ExitCode). $errorText"
}

if (-not $NoBrowser) {
    Start-Process $viewerUrl
}

if (-not $runFastChess) {
    $candidateStdout = [System.IO.Path]::ChangeExtension($pgnPath, $null) + "-launch.out.log"
    if ((-not $NoLearnerAutoLearn) -and (Test-Path $candidateStdout)) {
        $autolearnArgs = @($autolearnScript, "--pgn", $pgnPath, "--stdout", $candidateStdout, "--watch", "--interval", "10")
        $autolearnProcess = Start-Process `
            -FilePath "python" `
            -ArgumentList (ConvertTo-ProcessArguments -Arguments $autolearnArgs) `
            -WorkingDirectory $repoRoot `
            -RedirectStandardOutput $autolearnOut `
            -RedirectStandardError $autolearnErr `
            -WindowStyle Hidden `
            -PassThru
        Write-Host "Learner autolearn is running as process $($autolearnProcess.Id)."
    }
    Write-Host "Viewer is running at $viewerUrl"
    Write-Host "Stop it later with: Stop-Process -Id $($viewerProcess.Id)"
    return
}

$runParams = @{
    Games            = $Games
    Concurrency      = $Concurrency
    Model            = $Model
    Effort           = $Effort
    TimeControl      = $TimeControl
    FastChessVersion = $FastChessVersion
    RunName          = $runName
    Stamp            = $stamp
}
if ($MaxMoves -gt 0) {
    $runParams.MaxMoves = $MaxMoves
}
if ($ForceInstall) {
    $runParams.ForceInstall = $true
}
if ($SkipModelPreflight -or $modelAlreadyChecked) {
    $runParams.SkipModelPreflight = $true
}
if ($NoRepeat) {
    $runParams.NoRepeat = $true
}

$autolearnProcess = $null
if (-not $NoLearnerAutoLearn) {
    $autolearnArgs = @($autolearnScript, "--pgn", $pgnPath, "--stdout", $launchOut, "--watch", "--interval", "10")
    $autolearnProcess = Start-Process `
        -FilePath "python" `
        -ArgumentList (ConvertTo-ProcessArguments -Arguments $autolearnArgs) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $autolearnOut `
        -RedirectStandardError $autolearnErr `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "Learner autolearn is running as process $($autolearnProcess.Id)."
    Write-Host "Learner autolearn stdout: $autolearnOut"
}

try {
    & $runnerScript @runParams 2>&1 | Tee-Object -FilePath $launchOut
} finally {
    if ($StopViewerWhenDone -and -not $viewerProcess.HasExited) {
        Stop-Process -Id $viewerProcess.Id -Force
        Write-Host "Stopped local live viewer process $($viewerProcess.Id)."
    } elseif (-not $viewerProcess.HasExited) {
        Write-Host "Viewer is still running at $viewerUrl"
        Write-Host "Stop it later with: Stop-Process -Id $($viewerProcess.Id)"
    }
    if ($autolearnProcess -and -not $autolearnProcess.HasExited) {
        Write-Host "Learner autolearn is still running as process $($autolearnProcess.Id)."
    }
    if ($StopViewerWhenDone -and $mirrorProcess -and -not $mirrorProcess.HasExited) {
        Stop-Process -Id $mirrorProcess.Id -Force
        Write-Host "Stopped FastChess live mirror process $($mirrorProcess.Id)."
    } elseif ($mirrorProcess -and -not $mirrorProcess.HasExited) {
        Write-Host "FastChess live mirror is still running as process $($mirrorProcess.Id)."
    }
}
