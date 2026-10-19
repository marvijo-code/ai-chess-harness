"""Free OpenCode chess routes must stay inside the real client and never use tools or paid IDs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines' / 'llm-chess-engine'))
import subscription_providers as sp


def events(*rows):
    return '\n'.join(json.dumps(row) for row in rows)


class OpenCodeFreeTests(unittest.TestCase):
    def client(self, output):
        client = sp.SubscriptionChessClient('opencode', lambda _: None)
        client.model, client.board_image = sp.OPENCODE_FREE_MODELS[0], False
        client.runner = lambda *args: output
        return client

    @patch.object(sp, 'resolve_binary', return_value='opencode')
    def test_reply_session_usage_and_resume(self, _):
        output = events({'type':'step_start','sessionID':'ses_game'},
            {'type':'text','sessionID':'ses_game','part':{'text':'{"move":"e2e4"}'}},
            {'type':'step_finish','part':{'cost':0,'tokens':{'input':20,'output':5,'reasoning':7,
                'cache':{'read':80,'write':10}}}})
        client = self.client(output)
        client.new_conversation()
        calls = []
        client.runner = lambda *args: calls.append(args) or output
        self.assertEqual(json.loads(client._ask('position', 20))['move'], 'e2e4')
        client._ask('next position', 20)
        self.assertNotIn('--session', calls[0][0])
        self.assertNotIn('--auto', calls[0][0])
        self.assertIn('ses_game', calls[1][0])
        self.assertEqual(client._move_usage[0]['input'], 110)
        self.assertEqual(client._move_usage[0]['cached'], 80)
        self.assertEqual(client._move_usage[0]['output'], 12)

    @patch.object(sp, 'resolve_binary', return_value='opencode')
    def test_errors_tools_empty_output_and_charges_rejected(self, _):
        for output, error in [
            (events({'type':'error','error':{'message':'HTTP 403'}}), sp.ProviderError),
            (events({'type':'tool_use','part':{'type':'tool'}}), ValueError),
            ('startup only', sp.ProviderError),
            (events({'type':'text','part':{'text':'answer'}},
                    {'type':'step_finish','part':{'cost':0.01}}), sp.ProviderError),
        ]:
            with self.subTest(output=output), self.assertRaises(error):
                self.client(output)._ask('position', 20)

    def test_paid_ids_rejected_before_launch(self):
        client = self.client('')
        client.model = 'opencode/deepseek-v4.1-flash'
        with self.assertRaises(sp.ProviderError):
            client._ask('position', 20)

    def test_free_network_block_is_unavailable_not_a_chess_loss(self):
        message = "OpenCode's free models aren't available from your current network because we've detected abusive traffic"
        self.assertTrue(sp.provider_unavailable(message))
        client = self.client('')
        client.board_image, client.limit_wait, client.max_attempts = False, False, 1
        client.runner = lambda *args: (_ for _ in ()).throw(sp.ProviderError(message))
        import chess
        move, comment = client.choose_move(chess.Board(), {'wtime':60000,'btime':60000}, [])
        self.assertEqual(move, '0000')
        self.assertTrue(comment.startswith('provider unavailable:'))
        self.assertNotIn('forfeit', comment)

    def test_permission_override_and_paid_credentials_are_removed(self):
        stripped = sp.STRIPPED_ENV['opencode']
        self.assertIn('OPENCODE_PERMISSION', stripped)
        self.assertIn('OPENCODE_GO_API_KEY', stripped)
        self.assertIn('OPENROUTER_API_KEY', stripped)

    def test_isolated_configuration_denies_tools_and_pins_helpers(self):
        with tempfile.TemporaryDirectory() as folder:
            env = sp.opencode_environment(Path(folder), sp.OPENCODE_FREE_MODELS[1], 'chess rules')
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(cfg['permission'], {'*':'ask', 'external_directory':'deny'})
        self.assertEqual(cfg['agent']['chess']['permission'], cfg['permission'])
        self.assertEqual(cfg['agent']['chess']['prompt'], 'chess rules')
        self.assertEqual(cfg['model'], cfg['small_model'])
        self.assertFalse(cfg['compaction']['auto'])

    def test_reasoning_and_clock_from_real_events(self):
        line = events({'type':'reasoning','part':{'text':'Count the attackers.'}})
        self.assertEqual(sp.cli_visible_thinking(line), 'Count the attackers.')
        timing = [(10.0, '{"type":"step_start"}'), (12.5, '{"type":"step_finish"}')]
        self.assertEqual(sp.cli_think_ms('opencode', '', timing), 2500)


if __name__ == '__main__':
    unittest.main()
