param(
    [Alias("n")]
    [int]$Games = 10,
    [int]$Concurrency = 1,
    [string]$Model = "gpt-5.5",
    [string]$Effort = "low",
    [string]$TimeControl = "300+0",
    [int]$MaxMoves = 0,
    [string]$FastChessVersion = "latest",
    [int]$AnalysisMovetimeMs = 250,
    [int]$AnalysisMultipv = 3,
    [switch]$ForceInstall,
    [switch]$NoAnalysis,
    [switch]$NoBrowser,
    [switch]$StopViewerWhenDone,
    [switch]$NoLearnerAutoLearn,
    [switch]$SkipModelPreflight,
    [switch]$NoRepeat
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path $PSScriptRoot
$runner = Join-Path $repoRoot "watch-fastchess-live-match.ps1"
if (-not (Test-Path $runner)) {
    throw "FastChess live runner was not found: $runner"
}

$runParams = @{
    StartNewRun          = $true
    Games                = $Games
    Concurrency          = $Concurrency
    Model                = $Model
    Effort               = $Effort
    TimeControl          = $TimeControl
    FastChessVersion     = $FastChessVersion
    AnalysisMovetimeMs   = $AnalysisMovetimeMs
    AnalysisMultipv      = $AnalysisMultipv
}

if ($MaxMoves -gt 0) {
    $runParams.MaxMoves = $MaxMoves
}
if ($ForceInstall) {
    $runParams.ForceInstall = $true
}
if ($NoAnalysis) {
    $runParams.NoAnalysis = $true
}
if ($NoBrowser) {
    $runParams.NoBrowser = $true
}
if ($StopViewerWhenDone) {
    $runParams.StopViewerWhenDone = $true
}
if ($NoLearnerAutoLearn) {
    $runParams.NoLearnerAutoLearn = $true
}
if ($SkipModelPreflight) {
    $runParams.SkipModelPreflight = $true
}
if ($NoRepeat) {
    $runParams.NoRepeat = $true
}

& $runner @runParams
exit $LASTEXITCODE
