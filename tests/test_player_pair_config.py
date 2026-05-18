import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def ps_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def run_powershell(script: str) -> str:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"PowerShell failed with {completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return completed.stdout.strip()


class PlayerPairConfigTests(unittest.TestCase):
    def resolve_pair(self, bound: str, player1: str = "$null", player2: str = "$null", learning_engine: str = "$null"):
        script = f"""
$ErrorActionPreference = 'Stop'
. {ps_literal(ROOT / 'tools' / 'harness_config.ps1')}
$config = Get-ChessHarnessConfig -RepoRoot {ps_literal(ROOT)}
$pair = Resolve-ChessHarnessPlayers -BoundParameters {bound} -Player1 {player1} -Player2 {player2} -LearningEngine {learning_engine} -Config $config -RepoRoot {ps_literal(ROOT)}
[PSCustomObject]@{{
  Player1 = $pair.Player1.EngineName
  Player2 = $pair.Player2.EngineName
  AutoLearn = $pair.AutoLearnTarget.EngineName
  RunName = Get-ChessHarnessRunName -Config $config -Players $pair
  LiveRunName = Get-ChessHarnessRunName -Config $config -Players $pair -Live
}} | ConvertTo-Json -Compress
"""
        return json.loads(run_powershell(script).splitlines()[-1])

    def test_default_config_resolves_player1_and_player2(self):
        pair = self.resolve_pair("@{}")

        self.assertEqual(pair["Player1"], "Codex-chess-learner")
        self.assertEqual(pair["Player2"], "Codex-chess-zero")
        self.assertEqual(pair["AutoLearn"], "Codex-chess-zero")
        self.assertEqual(pair["RunName"], "codex-learner-vs-codex-zero")
        self.assertEqual(pair["LiveRunName"], "codex-learner-vs-codex-zero-live")

    def test_legacy_learning_engine_still_selects_zero_player2(self):
        pair = self.resolve_pair("@{ LearningEngine = 'zero' }", learning_engine="'zero'")

        self.assertEqual(pair["Player1"], "Codex-chess-learner")
        self.assertEqual(pair["Player2"], "Codex-chess-zero")
        self.assertEqual(pair["AutoLearn"], "Codex-chess-zero")
        self.assertEqual(pair["RunName"], "codex-learner-vs-codex-zero")

    def test_custom_player_order_is_supported(self):
        pair = self.resolve_pair("@{ Player1 = 'zero'; Player2 = 'learner' }", player1="'zero'", player2="'learner'")

        self.assertEqual(pair["Player1"], "Codex-chess-zero")
        self.assertEqual(pair["Player2"], "Codex-chess-learner")
        self.assertEqual(pair["AutoLearn"], "Codex-chess-learner")
        self.assertEqual(pair["RunName"], "codex-zero-vs-codex-learner")

    def test_learning_engine_conflict_is_rejected(self):
        script = f"""
$ErrorActionPreference = 'Stop'
. {ps_literal(ROOT / 'tools' / 'harness_config.ps1')}
$config = Get-ChessHarnessConfig -RepoRoot {ps_literal(ROOT)}
try {{
  Resolve-ChessHarnessPlayers -BoundParameters @{{ Player2 = 'learner'; LearningEngine = 'zero' }} -Player1 $null -Player2 'learner' -LearningEngine 'zero' -Config $config -RepoRoot {ps_literal(ROOT)} | Out-Null
  'NO_ERROR'
}} catch {{
  $_.Exception.Message
}}
"""
        self.assertIn("conflicts", run_powershell(script))

    def test_p_alias_binds_to_concurrency_on_runner_scripts(self):
        scripts = [
            ROOT / "play-games.ps1",
            ROOT / "watch-fastchess-live-match.ps1",
            ROOT / "install-and-run-fastchess-codex.ps1",
        ]
        quoted = "@(" + ",".join(ps_literal(path) for path in scripts) + ")"
        script = f"""
$ErrorActionPreference = 'Stop'
foreach ($path in {quoted}) {{
  $command = Get-Command $path
  $concurrencyAliases = @($command.Parameters['Concurrency'].Aliases)
  $player1Aliases = @($command.Parameters['Player1'].Aliases)
  $player2Aliases = @($command.Parameters['Player2'].Aliases)
  if ($concurrencyAliases -notcontains 'p') {{
    throw "Missing -p alias on Concurrency for $path"
  }}
  if ($player1Aliases -contains 'p' -or $player2Aliases -contains 'p') {{
    throw "-p alias must not be assigned to Player1 or Player2 for $path"
  }}
}}
'ok'
"""
        self.assertEqual(run_powershell(script).splitlines()[-1], "ok")

    def test_powershell_runner_scripts_parse(self):
        scripts = [
            ROOT / "play-games.ps1",
            ROOT / "watch-fastchess-live-match.ps1",
            ROOT / "install-and-run-fastchess-codex.ps1",
            ROOT / "tools" / "harness_config.ps1",
        ]
        quoted = "@(" + ",".join(ps_literal(path) for path in scripts) + ")"
        script = f"""
$ErrorActionPreference = 'Stop'
foreach ($path in {quoted}) {{
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
  if ($errors.Count -gt 0) {{
    throw ($errors | ForEach-Object {{ $_.Message }} | Out-String)
  }}
}}
'ok'
"""
        self.assertEqual(run_powershell(script).splitlines()[-1], "ok")


if __name__ == "__main__":
    unittest.main()
