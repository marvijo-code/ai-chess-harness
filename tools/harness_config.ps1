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
