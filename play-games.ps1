param(
    [Alias("n")]
    [Nullable[int]]$Games = $null,
    [Nullable[int]]$Concurrency = $null,
    [string]$Model = $null,
    [string]$Effort = $null,
    [string]$TimeControl = $null,
    [Nullable[int]]$MaxMoves = $null,
    [string]$FastChessVersion = $null,
    [Nullable[int]]$AnalysisMovetimeMs = $null,
    [Nullable[int]]$AnalysisMultipv = $null,
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
. (Join-Path $repoRoot "tools\harness_config.ps1")
$config = Get-ChessHarnessConfig -RepoRoot $repoRoot

$Games = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Games" -CurrentValue $Games -Config $config -Path "fastChess.games" -Default 10)
$Concurrency = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Concurrency" -CurrentValue $Concurrency -Config $config -Path "fastChess.concurrency" -Default 1)
$Model = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Model" -CurrentValue $Model -Config $config -Path "codex.model" -Default "gpt-5.3-codex")
$Effort = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Effort" -CurrentValue $Effort -Config $config -Path "codex.effort" -Default "high")
$TimeControl = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "TimeControl" -CurrentValue $TimeControl -Config $config -Path "fastChess.timeControl" -Default "300+0")
$MaxMoves = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "MaxMoves" -CurrentValue $MaxMoves -Config $config -Path "fastChess.maxMoves" -Default 0)
$FastChessVersion = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "FastChessVersion" -CurrentValue $FastChessVersion -Config $config -Path "fastChess.version" -Default "latest")
$AnalysisMovetimeMs = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "AnalysisMovetimeMs" -CurrentValue $AnalysisMovetimeMs -Config $config -Path "viewer.analysisMovetimeMs" -Default 250)
$AnalysisMultipv = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "AnalysisMultipv" -CurrentValue $AnalysisMultipv -Config $config -Path "viewer.analysisMultipv" -Default 3)
$forceInstallEnabled = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "ForceInstall" -CurrentValue $ForceInstall -Config $config -Path "fastChess.forceInstall" -Default $false
$analysisDisabled = if ($PSBoundParameters.ContainsKey("NoAnalysis")) { [bool]$NoAnalysis } else { -not [bool](Get-HarnessConfigValue -Config $config -Path "viewer.analysisEnabled" -Default $true) }
$browserDisabled = if ($PSBoundParameters.ContainsKey("NoBrowser")) { [bool]$NoBrowser } else { -not [bool](Get-HarnessConfigValue -Config $config -Path "viewer.openBrowser" -Default $true) }
$stopViewerWhenDoneEnabled = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "StopViewerWhenDone" -CurrentValue $StopViewerWhenDone -Config $config -Path "viewer.stopWhenDone" -Default $false
$learnerAutoLearnDisabled = if ($PSBoundParameters.ContainsKey("NoLearnerAutoLearn")) { [bool]$NoLearnerAutoLearn } else { -not [bool](Get-HarnessConfigValue -Config $config -Path "learner.autoLearn" -Default $true) }
$skipModelPreflightEnabled = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "SkipModelPreflight" -CurrentValue $SkipModelPreflight -Config $config -Path "fastChess.skipModelPreflight" -Default $false
$noRepeatEnabled = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "NoRepeat" -CurrentValue $NoRepeat -Config $config -Path "fastChess.noRepeat" -Default $false

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
if ($forceInstallEnabled) {
    $runParams.ForceInstall = $true
}
if ($analysisDisabled) {
    $runParams.NoAnalysis = $true
}
if ($browserDisabled) {
    $runParams.NoBrowser = $true
}
if ($stopViewerWhenDoneEnabled) {
    $runParams.StopViewerWhenDone = $true
}
if ($learnerAutoLearnDisabled) {
    $runParams.NoLearnerAutoLearn = $true
}
if ($skipModelPreflightEnabled) {
    $runParams.SkipModelPreflight = $true
}
if ($noRepeatEnabled) {
    $runParams.NoRepeat = $true
}

& $runner @runParams
exit $LASTEXITCODE
