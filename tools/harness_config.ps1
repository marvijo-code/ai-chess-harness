function Get-ChessHarnessConfig {
    param([string]$RepoRoot)

    $configPath = Join-Path $RepoRoot "chess-harness.config.json"
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "Chess harness config was not found: $configPath"
    }

    try {
        return Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    } catch {
        throw "Chess harness config is not valid JSON: $configPath. $($_.Exception.Message)"
    }
}

function Get-HarnessConfigValue {
    param(
        [object]$Config,
        [string]$Path,
        [object]$Default = $null
    )

    $current = $Config
    foreach ($part in ($Path -split '\.')) {
        if ($null -eq $current) {
            return $Default
        }
        $property = $current.PSObject.Properties[$part]
        if ($null -eq $property) {
            return $Default
        }
        $current = $property.Value
    }

    if ($null -eq $current) {
        return $Default
    }
    return $current
}

function Resolve-HarnessSetting {
    param(
        [hashtable]$BoundParameters,
        [string]$Name,
        [object]$CurrentValue,
        [object]$Config,
        [string]$Path,
        [object]$Default = $null
    )

    if ($BoundParameters.ContainsKey($Name) -and $null -ne $CurrentValue) {
        return $CurrentValue
    }
    return Get-HarnessConfigValue -Config $Config -Path $Path -Default $Default
}

function Resolve-HarnessSwitch {
    param(
        [hashtable]$BoundParameters,
        [string]$Name,
        [switch]$CurrentValue,
        [object]$Config,
        [string]$Path,
        [bool]$Default = $false
    )

    if ($BoundParameters.ContainsKey($Name)) {
        return [bool]$CurrentValue
    }
    return [bool](Get-HarnessConfigValue -Config $Config -Path $Path -Default $Default)
}

function Get-ChessHarnessEngineSpec {
    param(
        [AllowNull()][string]$Name,
        [string]$RepoRoot
    )

    $normalized = if ($null -eq $Name) { "" } else { $Name.Trim().ToLowerInvariant() }
    if ($normalized -eq "") {
        throw "Engine name cannot be empty. Use one of: codex, learner, zero."
    }

    if (@("codex", "codex-chess", "base", "baseline") -contains $normalized) {
        return [PSCustomObject]@{
            Key        = "codex"
            EngineName = "Codex-chess"
            Command    = Join-Path $RepoRoot "engines\codex-chess\codex-chess.cmd"
            ContextDir = Join-Path $RepoRoot "engines\codex-chess"
            IsLearning = $false
        }
    }
    if (@("learner", "codex-chess-learner") -contains $normalized) {
        return [PSCustomObject]@{
            Key        = "learner"
            EngineName = "Codex-chess-learner"
            Command    = Join-Path $RepoRoot "engines\codex-chess-learner\codex-chess-learner.cmd"
            ContextDir = Join-Path $RepoRoot "engines\codex-chess-learner"
            IsLearning = $true
        }
    }
    if (@("zero", "codex-chess-zero") -contains $normalized) {
        return [PSCustomObject]@{
            Key        = "zero"
            EngineName = "Codex-chess-zero"
            Command    = Join-Path $RepoRoot "engines\codex-chess-zero\codex-chess-zero.cmd"
            ContextDir = Join-Path $RepoRoot "engines\codex-chess-zero"
            IsLearning = $true
        }
    }

    throw "Unsupported engine '$Name'. Use one of: codex, learner, zero."
}

function Resolve-ChessHarnessPlayers {
    param(
        [hashtable]$BoundParameters,
        [AllowNull()][string]$Player1,
        [AllowNull()][string]$Player2,
        [AllowNull()][string]$LearningEngine,
        [object]$Config,
        [string]$RepoRoot
    )

    $resolvedPlayer1 = [string](Resolve-HarnessSetting -BoundParameters $BoundParameters -Name "Player1" -CurrentValue $Player1 -Config $Config -Path "fastChess.player1" -Default "codex")
    $legacyDefault = [string](Get-HarnessConfigValue -Config $Config -Path "fastChess.learningEngine" -Default "learner")
    $player2Default = [string](Get-HarnessConfigValue -Config $Config -Path "fastChess.player2" -Default $legacyDefault)
    $resolvedPlayer2 = [string](Resolve-HarnessSetting -BoundParameters $BoundParameters -Name "Player2" -CurrentValue $Player2 -Config $Config -Path "fastChess.player2" -Default $player2Default)

    if ($BoundParameters.ContainsKey("LearningEngine") -and $null -ne $LearningEngine) {
        if ($BoundParameters.ContainsKey("Player2") -and $null -ne $Player2) {
            $legacySpec = Get-ChessHarnessEngineSpec -Name $LearningEngine -RepoRoot $RepoRoot
            $player2SpecForConflictCheck = Get-ChessHarnessEngineSpec -Name $resolvedPlayer2 -RepoRoot $RepoRoot
            if ($legacySpec.Key -ne $player2SpecForConflictCheck.Key) {
                throw "-LearningEngine '$LearningEngine' conflicts with -Player2 '$Player2'. Use -Player2 for the new form, or omit -Player2 for legacy compatibility."
            }
        } else {
            $resolvedPlayer2 = $LearningEngine
        }
    }

    $player1Spec = Get-ChessHarnessEngineSpec -Name $resolvedPlayer1 -RepoRoot $RepoRoot
    $player2Spec = Get-ChessHarnessEngineSpec -Name $resolvedPlayer2 -RepoRoot $RepoRoot
    if ($player1Spec.Key -eq $player2Spec.Key) {
        throw "Player1 and Player2 must be different engines. Both resolved to $($player1Spec.EngineName)."
    }

    $autoLearnTarget = $null
    if ($player2Spec.IsLearning) {
        $autoLearnTarget = $player2Spec
    } elseif ($player1Spec.IsLearning) {
        $autoLearnTarget = $player1Spec
    }

    return [PSCustomObject]@{
        Player1         = $player1Spec
        Player2         = $player2Spec
        AutoLearnTarget = $autoLearnTarget
    }
}

function Get-ChessHarnessRunName {
    param(
        [object]$Config,
        [object]$Players,
        [switch]$Live
    )

    if ($Players.Player1.Key -eq "codex" -and $Players.Player2.Key -eq "learner") {
        if ($Live) {
            return [string](Get-HarnessConfigValue -Config $Config -Path "fastChess.liveRunName" -Default "codex-vs-codex-learner-live")
        }
        return [string](Get-HarnessConfigValue -Config $Config -Path "fastChess.runName" -Default "codex-vs-codex-learner")
    }
    if ($Players.Player1.Key -eq "codex" -and $Players.Player2.Key -eq "zero") {
        if ($Live) {
            return [string](Get-HarnessConfigValue -Config $Config -Path "fastChess.zeroLiveRunName" -Default "codex-vs-codex-zero-live")
        }
        return [string](Get-HarnessConfigValue -Config $Config -Path "fastChess.zeroRunName" -Default "codex-vs-codex-zero")
    }

    $slugByKey = @{
        codex   = "codex"
        learner = "codex-learner"
        zero    = "codex-zero"
    }
    $suffix = if ($Live) { "-live" } else { "" }
    return "$($slugByKey[$Players.Player1.Key])-vs-$($slugByKey[$Players.Player2.Key])$suffix"
}
