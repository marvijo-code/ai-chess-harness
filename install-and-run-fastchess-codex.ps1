param(
    [int]$Games = 10,
    [int]$Concurrency = 1,
    [string]$Model = "gpt-5.3-codex-spark",
    [string]$Effort = "low",
    [string]$TimeControl = "300+0",
    [int]$MaxMoves = 0,
    [string]$FastChessVersion = "latest",
    [switch]$ForceInstall
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

$repoRoot = Resolve-RepoRoot
$installRoot = Join-Path $repoRoot "tools\.fastchess"
$resultsRoot = Join-Path $repoRoot "out\fastchess"
$codexEngine = Join-Path $repoRoot "engines\codex-chess\codex-chess.cmd"
$learnerEngine = Join-Path $repoRoot "engines\codex-chess-learner\codex-chess-learner.cmd"

if (-not (Test-Path $codexEngine)) {
    throw "Codex-chess engine command was not found: $codexEngine"
}
if (-not (Test-Path $learnerEngine)) {
    throw "Codex-chess-learner engine command was not found: $learnerEngine"
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

if ($Games -lt 2) {
    throw "-Games must be at least 2 because FastChess repeats paired colors."
}

$rounds = [Math]::Ceiling($Games / 2)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$pgnPath = Join-Path $resultsRoot "codex-vs-codex-learner-$stamp.pgn"
$configPath = Join-Path $resultsRoot "codex-vs-codex-learner-$stamp-config.json"
$logPath = Join-Path $resultsRoot "codex-vs-codex-learner-$stamp.log"

$fastChess = Install-FastChess -Version $FastChessVersion

$env:CODEX_CHESS_MODEL = $Model
$env:CODEX_CHESS_EFFORT = $Effort

Write-Host "FastChess: $fastChess"
Write-Host "Engine model override: $Model"
Write-Host "Effort: $Effort"
Write-Host "TimeControl: $TimeControl"
Write-Host "Games requested: $Games"
Write-Host "Rounds: $rounds with -repeat, so FastChess will schedule $($rounds * 2) games."
if ($MaxMoves -gt 0) {
    Write-Host "MaxMoves: $MaxMoves"
} else {
    Write-Host "MaxMoves: disabled"
}
Write-Host "PGN: $pgnPath"
Write-Host "Log: $logPath"

$fastChessArgs = @(
    "-engine", "cmd=$codexEngine", "name=Codex-chess", "proto=uci", "restart=on",
    "-engine", "cmd=$learnerEngine", "name=Codex-chess-learner", "proto=uci", "restart=on",
    "-each", "tc=$TimeControl", "timemargin=5000",
    "-rounds", "$rounds",
    "-repeat",
    "-concurrency", "$Concurrency",
    "-ratinginterval", "1",
    "-pgnout", "file=$pgnPath", "notation=san", "append=false", "timeleft=true", "latency=true",
    "-config", "outname=$configPath", "stats=true",
    "-autosaveinterval", "2",
    "-recover",
    "-log", "file=$logPath", "level=info", "append=false"
)

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
