param(
    [Nullable[int]]$Games = $null,
    [Nullable[int]]$Concurrency = $null,
    [string]$Model = $null,
    [string]$Effort = $null,
    [string]$TimeControl = $null,
    [Nullable[int]]$MaxMoves = $null,
    [string]$FastChessVersion = $null,
    [ValidateSet("learner", "zero")]
    [string]$LearningEngine = $null,
    [string]$RunName = $null,
    [string]$Stamp = "",
    [switch]$ForceInstall,
    [switch]$SkipModelPreflight,
    [switch]$NoRepeat
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-RepoRoot {
    $candidate = Resolve-Path $PSScriptRoot
    if (Test-Path (Join-Path $candidate "engines\codex-chess\codex-chess.cmd")) {
        return $candidate
    }

    $parent = Resolve-Path (Join-Path $PSScriptRoot "..")
    if (Test-Path (Join-Path $parent "engines\codex-chess\codex-chess.cmd")) {
        return $parent
    }

    throw "Could not resolve repo root from script path: $PSScriptRoot"
}

function Get-LearningEngineSpec {
    param(
        [string]$Name,
        [string]$RepoRoot
    )

    switch ($Name.ToLowerInvariant()) {
        "learner" {
            return [PSCustomObject]@{
                Key        = "learner"
                EngineName = "Codex-chess-learner"
                Command    = Join-Path $RepoRoot "engines\codex-chess-learner\codex-chess-learner.cmd"
                ContextDir = Join-Path $RepoRoot "engines\codex-chess-learner"
            }
        }
        "zero" {
            return [PSCustomObject]@{
                Key        = "zero"
                EngineName = "Codex-chess-zero"
                Command    = Join-Path $RepoRoot "engines\codex-chess-zero\codex-chess-zero.cmd"
                ContextDir = Join-Path $RepoRoot "engines\codex-chess-zero"
            }
        }
        default {
            throw "Unsupported learning engine: $Name"
        }
    }
}

$repoRoot = Resolve-RepoRoot
. (Join-Path $repoRoot "tools\harness_config.ps1")
$config = Get-ChessHarnessConfig -RepoRoot $repoRoot

$Games = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Games" -CurrentValue $Games -Config $config -Path "fastChess.games" -Default 10)
$Concurrency = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Concurrency" -CurrentValue $Concurrency -Config $config -Path "fastChess.concurrency" -Default 1)
$Model = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Model" -CurrentValue $Model -Config $config -Path "codex.model" -Default "gpt-5.3-codex")
$Effort = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "Effort" -CurrentValue $Effort -Config $config -Path "codex.effort" -Default "high")
$TimeControl = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "TimeControl" -CurrentValue $TimeControl -Config $config -Path "fastChess.timeControl" -Default "300+0")
$MaxMoves = [int](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "MaxMoves" -CurrentValue $MaxMoves -Config $config -Path "fastChess.maxMoves" -Default 0)
$FastChessVersion = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "FastChessVersion" -CurrentValue $FastChessVersion -Config $config -Path "fastChess.version" -Default "latest")
$LearningEngine = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "LearningEngine" -CurrentValue $LearningEngine -Config $config -Path "fastChess.learningEngine" -Default "learner")
$learningEngineSpec = Get-LearningEngineSpec -Name $LearningEngine -RepoRoot $repoRoot
$runNameConfigPath = if ($learningEngineSpec.Key -eq "zero") { "fastChess.zeroRunName" } else { "fastChess.runName" }
$runNameDefault = if ($learningEngineSpec.Key -eq "zero") { "codex-vs-codex-zero" } else { "codex-vs-codex-learner" }
$RunName = [string](Resolve-HarnessSetting -BoundParameters $PSBoundParameters -Name "RunName" -CurrentValue $RunName -Config $config -Path $runNameConfigPath -Default $runNameDefault)
$ForceInstall = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "ForceInstall" -CurrentValue $ForceInstall -Config $config -Path "fastChess.forceInstall" -Default $false
$SkipModelPreflight = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "SkipModelPreflight" -CurrentValue $SkipModelPreflight -Config $config -Path "fastChess.skipModelPreflight" -Default $false
$NoRepeat = Resolve-HarnessSwitch -BoundParameters $PSBoundParameters -Name "NoRepeat" -CurrentValue $NoRepeat -Config $config -Path "fastChess.noRepeat" -Default $false
$timeMarginMs = [int](Get-HarnessConfigValue -Config $config -Path "fastChess.timeMarginMs" -Default 5000)
$ratingInterval = [int](Get-HarnessConfigValue -Config $config -Path "fastChess.ratingInterval" -Default 1)
$autosaveInterval = [int](Get-HarnessConfigValue -Config $config -Path "fastChess.autosaveInterval" -Default 2)
$logLevel = [string](Get-HarnessConfigValue -Config $config -Path "fastChess.logLevel" -Default "info")

$installRoot = Join-Path $repoRoot "tools\.fastchess"
$resultsRoot = Join-Path $repoRoot "out\fastchess"
$codexEngine = Join-Path $repoRoot "engines\codex-chess\codex-chess.cmd"
$learningEngineCommand = $learningEngineSpec.Command

if (-not (Test-Path $codexEngine)) {
    throw "Codex-chess engine command was not found: $codexEngine"
}
if (-not (Test-Path $learningEngineCommand)) {
    throw "$($learningEngineSpec.EngineName) engine command was not found: $learningEngineCommand"
}

New-Item -ItemType Directory -Force -Path $installRoot, $resultsRoot | Out-Null

function Get-FastChessRelease {
    param([string]$Version)

    if ($Version -eq "latest") {
        $release = gh release view --repo Disservin/fastchess --json tagName,assets | ConvertFrom-Json
    } else {
        $release = gh release view $Version --repo Disservin/fastchess --json tagName,assets | ConvertFrom-Json
    }

    $asset = $release.assets | Where-Object { $_.name -eq "fastchess-windows-x86-64.zip" } | Select-Object -First 1
    if (-not $asset) {
        throw "Release $($release.tagName) does not contain fastchess-windows-x86-64.zip"
    }

    [PSCustomObject]@{
        Tag = $release.tagName
        AssetName = $asset.name
        Url = $asset.url
    }
}

function Install-FastChess {
    param([string]$Version)

    $release = Get-FastChessRelease -Version $Version
    $versionRoot = Join-Path $installRoot $release.Tag
    $fastChessExe = Join-Path $versionRoot "fastchess.exe"
    $stampPath = Join-Path $versionRoot ".installed"

    if ((Test-Path $fastChessExe) -and (Test-Path $stampPath) -and -not $ForceInstall) {
        return $fastChessExe
    }

    if (Test-Path $versionRoot) {
        Remove-Item -LiteralPath $versionRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $versionRoot | Out-Null

    $zipPath = Join-Path $installRoot $release.AssetName
    gh release download $release.Tag --repo Disservin/fastchess --pattern $release.AssetName --output $zipPath --clobber
    Expand-Archive -LiteralPath $zipPath -DestinationPath $versionRoot -Force
    Remove-Item -LiteralPath $zipPath -Force

    $foundExe = Get-ChildItem -LiteralPath $versionRoot -Filter "fastchess.exe" -Recurse | Select-Object -First 1
    if (-not $foundExe) {
        throw "fastchess.exe was not found after extracting $($release.AssetName)"
    }
    if ($foundExe.FullName -ne $fastChessExe) {
        Move-Item -LiteralPath $foundExe.FullName -Destination $fastChessExe -Force
    }

    Set-Content -LiteralPath $stampPath -Value "tag=$($release.Tag)`ninstalled=$(Get-Date -Format o)" -Encoding utf8
    return $fastChessExe
}

if ($Games -lt 1) {
    throw "-Games must be at least 1."
}

$rounds = if ($NoRepeat) { $Games } else { [Math]::Ceiling($Games / 2) }
if ($RunName -match '[\\/:*?"<>|]') {
    throw "-RunName must be a file-name-safe value, without path separators or reserved characters."
}
if ($Stamp -eq "") {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
} else {
    if ($Stamp -match '[\\/:*?"<>|]') {
        throw "-Stamp must be a file-name-safe value, without path separators or reserved characters."
    }
    $stamp = $Stamp
}
$outputBaseName = "$RunName-$stamp"
$pgnPath = Join-Path $resultsRoot "$outputBaseName.pgn"
$configPath = Join-Path $resultsRoot "$outputBaseName-config.json"
$logPath = Join-Path $resultsRoot "$outputBaseName.log"

$fastChess = Install-FastChess -Version $FastChessVersion
$preflight = Join-Path $repoRoot "tools\check_codex_model_available.py"

if (-not $SkipModelPreflight) {
    Write-Host "Checking Codex model availability: $Model (reasoning effort: $Effort)"
    & python $preflight --model $Model --effort $Effort
    if ($LASTEXITCODE -ne 0) {
        throw "Codex model preflight failed for $Model with reasoning effort $Effort. Choose another -Model/-Effort or wait for the model limit reset before starting FastChess."
    }
}

$env:CODEX_CHESS_MODEL = $Model
$env:CODEX_CHESS_EFFORT = $Effort

Write-Host "FastChess: $fastChess"
Write-Host "Engine model override: $Model"
Write-Host "Effort: $Effort"
Write-Host "Learning engine: $($learningEngineSpec.EngineName)"
Write-Host "TimeControl: $TimeControl"
Write-Host "Games requested: $Games"
if ($NoRepeat) {
    Write-Host "Rounds: $rounds without -repeat, so FastChess will schedule exactly $rounds games."
} else {
    Write-Host "Rounds: $rounds with -repeat, so FastChess will schedule $($rounds * 2) games."
}
if ($MaxMoves -gt 0) {
    Write-Host "MaxMoves: $MaxMoves"
} else {
    Write-Host "MaxMoves: disabled"
}
Write-Host "PGN: $pgnPath"
Write-Host "Log: $logPath"

$fastChessArgs = @(
    "-engine", "cmd=$codexEngine", "name=Codex-chess", "proto=uci", "restart=on",
    "-engine", "cmd=$learningEngineCommand", "name=$($learningEngineSpec.EngineName)", "proto=uci", "restart=on",
    "-each", "tc=$TimeControl", "timemargin=$timeMarginMs",
    "-rounds", "$rounds",
    "-concurrency", "$Concurrency",
    "-ratinginterval", "$ratingInterval",
    "-pgnout", "file=$pgnPath", "notation=san", "append=false", "timeleft=true", "latency=true",
    "-config", "outname=$configPath", "stats=true",
    "-autosaveinterval", "$autosaveInterval",
    "-recover",
    "-log", "file=$logPath", "level=$logLevel", "append=false"
)

if (-not $NoRepeat) {
    $repeatInsertIndex = [Array]::IndexOf($fastChessArgs, "-concurrency")
    $fastChessArgs = @(
        $fastChessArgs[0..($repeatInsertIndex - 1)]
        "-repeat"
        $fastChessArgs[$repeatInsertIndex..($fastChessArgs.Count - 1)]
    )
}

if ($MaxMoves -gt 0) {
    $concurrencyIndex = [Array]::IndexOf($fastChessArgs, "-concurrency")
    $insertIndex = $concurrencyIndex + 2
    $fastChessArgs = @(
        $fastChessArgs[0..($insertIndex - 1)]
        "-maxmoves"
        "$MaxMoves"
        $fastChessArgs[$insertIndex..($fastChessArgs.Count - 1)]
    )
}

& $fastChess @fastChessArgs

if ($LASTEXITCODE -ne 0) {
    throw "FastChess exited with code $LASTEXITCODE"
}

Write-Host "FastChess run complete."
Write-Host "PGN written to $pgnPath"
Write-Host "Config written to $configPath"
