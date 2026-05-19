[CmdletBinding()]
param(
    [string]$Profile = "gm-sprint",
    [int]$MaxRounds = 0,
    [double]$SleepSeconds = 0,
    [double]$FailureSleepSeconds = 60,
    [int]$MaxConsecutiveFailures = 3,
    [switch]$ForceStaleLock,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RoundArgs
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$loopArgs = @(
    ".\tools\run_zero_climb_loop.py",
    "--profile", $Profile,
    "--max-rounds", $MaxRounds,
    "--sleep-seconds", $SleepSeconds,
    "--failure-sleep-seconds", $FailureSleepSeconds,
    "--max-consecutive-failures", $MaxConsecutiveFailures
)
if ($ForceStaleLock) {
    $loopArgs += "--force-stale-lock"
}
if ($RoundArgs) {
    $loopArgs += $RoundArgs
}

& python @loopArgs
exit $LASTEXITCODE
