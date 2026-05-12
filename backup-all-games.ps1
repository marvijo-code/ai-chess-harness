param(
    [string]$BackupName = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-RepoRoot {
    $candidate = Resolve-Path $PSScriptRoot
    if (Test-Path (Join-Path $candidate "tools\live_pgn_viewer.py")) {
        return $candidate
    }

    throw "Could not resolve repo root from script path: $PSScriptRoot"
}

function Get-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Get-RelativePath {
    param(
        [string]$Root,
        [string]$Path
    )

    $rootFull = (Get-FullPath $Root).TrimEnd([char[]]"\/")
    $pathFull = Get-FullPath $Path
    if ($pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $pathFull.Substring($rootFull.Length).TrimStart([char[]]"\/")
    }
    return $pathFull
}

function Get-ArtifactSummary {
    param([string]$Path)

    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        return [PSCustomObject]@{
            Kind = "file"
            FileCount = 1
            Bytes = [int64]$item.Length
        }
    }

    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue)
    $bytes = [int64]0
    foreach ($file in $files) {
        $bytes += [int64]$file.Length
    }
    return [PSCustomObject]@{
        Kind = "directory"
        FileCount = $files.Count
        Bytes = $bytes
    }
}

function Move-BackupArtifact {
    param(
        [string]$Source,
        [string]$Destination,
        [string]$RepoRoot,
        [System.Collections.Generic.List[object]]$ManifestEntries,
        [System.Collections.Generic.HashSet[string]]$SeenSources
    )

    $fullSource = Get-FullPath $Source
    if (-not (Test-Path -LiteralPath $fullSource)) {
        return
    }
    if (-not $SeenSources.Add($fullSource)) {
        return
    }

    $summary = Get-ArtifactSummary -Path $fullSource
    $fullDestination = Get-FullPath $Destination
    $entry = [PSCustomObject]@{
        source = Get-RelativePath -Root $RepoRoot -Path $fullSource
        destination = Get-RelativePath -Root $RepoRoot -Path $fullDestination
        kind = $summary.Kind
        fileCount = $summary.FileCount
        bytes = $summary.Bytes
    }
    $ManifestEntries.Add($entry)

    if ($DryRun) {
        return
    }

    $destinationParent = Split-Path -Parent $fullDestination
    New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null
    Move-Item -LiteralPath $fullSource -Destination $fullDestination -Force
}

$repoRoot = Resolve-RepoRoot
$outRoot = Join-Path $repoRoot "out"
$backupRootBase = Join-Path $outRoot "backups"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
if ([string]::IsNullOrWhiteSpace($BackupName)) {
    $BackupName = "games-reset-$timestamp"
}
$backupRoot = Join-Path $backupRootBase $BackupName

if (-not (Test-Path -LiteralPath $outRoot)) {
    New-Item -ItemType Directory -Force -Path $outRoot | Out-Null
}
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
}

$manifestEntries = [System.Collections.Generic.List[object]]::new()
$seenSources = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

$activeDirectories = @(
    "out\fastchess",
    "out\live",
    "out\codex-chess-logs",
    "out\llm-chess-engine-logs"
)

foreach ($relativeDirectory in $activeDirectories) {
    $sourceDirectory = Join-Path $repoRoot $relativeDirectory
    if (-not (Test-Path -LiteralPath $sourceDirectory)) {
        continue
    }

    $targetDirectory = Join-Path $backupRoot (Split-Path -Leaf $sourceDirectory)
    foreach ($child in Get-ChildItem -LiteralPath $sourceDirectory -Force -ErrorAction SilentlyContinue) {
        Move-BackupArtifact `
            -Source $child.FullName `
            -Destination (Join-Path $targetDirectory $child.Name) `
            -RepoRoot $repoRoot `
            -ManifestEntries $manifestEntries `
            -SeenSources $seenSources
    }
}

$rootGameCompanionExtensions = @(".pgn", ".json", ".png", ".log", ".txt")
$rootPgns = @(Get-ChildItem -LiteralPath $outRoot -Filter "*.pgn" -File -Force -ErrorAction SilentlyContinue)
foreach ($pgn in $rootPgns) {
    foreach ($extension in $rootGameCompanionExtensions) {
        $candidate = Join-Path $outRoot ($pgn.BaseName + $extension)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            Move-BackupArtifact `
                -Source $candidate `
                -Destination (Join-Path (Join-Path $backupRoot "root") (Split-Path -Leaf $candidate)) `
                -RepoRoot $repoRoot `
                -ManifestEntries $manifestEntries `
                -SeenSources $seenSources
        }
    }
}

if (-not $DryRun) {
    foreach ($relativeDirectory in $activeDirectories) {
        New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot $relativeDirectory) | Out-Null
    }

    $manifest = [PSCustomObject]@{
        createdAt = (Get-Date).ToString("o")
        repoRoot = [string]$repoRoot
        backupRoot = [string](Resolve-Path $backupRoot)
        movedArtifacts = $manifestEntries.Count
        movedFiles = [int](($manifestEntries | Measure-Object -Property fileCount -Sum).Sum)
        movedBytes = [int64](($manifestEntries | Measure-Object -Property bytes -Sum).Sum)
        artifacts = $manifestEntries
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $backupRoot "manifest.json") -Encoding UTF8
}

$totalFiles = [int](($manifestEntries | Measure-Object -Property fileCount -Sum).Sum)
$totalBytes = [int64](($manifestEntries | Measure-Object -Property bytes -Sum).Sum)
$mode = if ($DryRun) { "Would move" } else { "Moved" }
Write-Host "$mode $($manifestEntries.Count) artifact entries ($totalFiles files, $totalBytes bytes)."
Write-Host "Backup path: $backupRoot"
