import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bridge', ROOT / 'asf-bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        bridge.config = {'bot_name': 'target bot', 'play_appids': [123],
                         'play_game_names': {'123': 'Game'}, 'reconnect_timeout': 2}

    def test_empty_plugin_result_is_authoritative(self):
        with patch.object(bridge, 'bot_info', return_value={'IsConnectedAndLoggedOn': True}), \
             patch.object(bridge, 'plugin_play_status', return_value={'CurrentGames': [], 'Source': 'None'}):
            status = bridge.build_status()
        self.assertTrue(status['play_known'])
        self.assertEqual(status['playing'], [])
        self.assertEqual(status['target_games'][0]['AppID'], 123)

    def test_unavailable_plugin_reports_unknown_not_target_as_playing(self):
        with patch.object(bridge, 'bot_info', return_value={'IsConnectedAndLoggedOn': True}), \
             patch.object(bridge, 'plugin_play_status', return_value=None):
            status = bridge.build_status()
        self.assertFalse(status['play_known'])
        self.assertEqual(status['playing'], [])

    def test_disconnected_bot_has_no_playing_games(self):
        with patch.object(bridge, 'bot_info', return_value={'IsConnectedAndLoggedOn': False}), \
             patch.object(bridge, 'plugin_play_status', return_value={'CurrentGames': [{'AppID': 123}], 'Source': 'Manual'}):
            self.assertEqual(bridge.build_status()['playing'], [])

    def test_query_selects_bot_and_refuses_unfiltered_multi_bot_reply(self):
        reply = {'Success': True, 'Result': {'bot1': {'CurrentGames': []}, 'bot2': {'CurrentGames': []}}}
        with patch.object(bridge, 'asf_request', return_value=(200, reply)) as request:
            self.assertIsNone(bridge.plugin_play_status())
        request.assert_called_once_with('GET', '/api/playstatus?Bot=target+bot')

    def test_plugin_business_error_does_not_report_known(self):
        with patch.object(bridge, 'asf_request', return_value=(200, {'Success': False, 'Result': {'bot1': {'CurrentGames': []}}})):
            self.assertIsNone(bridge.plugin_play_status())

    def test_bot_name_is_url_escaped_and_missing_target_is_not_reachable(self):
        with patch.object(bridge, 'asf_request', return_value=(200, {'Result': {}})) as request:
            self.assertIsNone(bridge.bot_info())
        request.assert_called_once_with('GET', '/api/bot/target%20bot')

    def test_http_200_business_failure_raises(self):
        with patch.object(bridge, 'asf_request', return_value=(200, {'Success': False, 'Message': 'not connected'})):
            with self.assertRaisesRegex(RuntimeError, 'not connected'):
                bridge.play_configured_games()

    def test_offline_timeout_never_sends_play(self):
        with patch.object(bridge, 'bot_info', return_value={'IsConnectedAndLoggedOn': False}), \
             patch.object(bridge, 'plugin_play_status', return_value=None), \
             patch.object(bridge, 'asf_action', return_value={'Success': True}) as action, \
             patch.object(bridge.time, 'monotonic', side_effect=[0, 0.1, 3]), \
             patch.object(bridge.time, 'sleep'):
            status = bridge.do_reconnect()
        self.assertFalse(status['success'])
        action.assert_called_once_with('/api/asf/restart')

    def test_waits_for_login_and_plays_through_structured_endpoint(self):
        flags = [{'IsConnectedAndLoggedOn': False}, {'IsConnectedAndLoggedOn': False},
                 {'IsConnectedAndLoggedOn': True}, {'IsConnectedAndLoggedOn': True}]
        with patch.object(bridge, 'bot_info', side_effect=flags), \
             patch.object(bridge, 'plugin_play_status', return_value={'CurrentGames': [{'AppID': 123}], 'Source': 'Manual'}), \
             patch.object(bridge, 'asf_request', return_value=(200, {'Success': True})) as request, \
             patch.object(bridge.time, 'monotonic', side_effect=[0, 0.1, 0.2]), \
             patch.object(bridge.time, 'sleep'):
            status = bridge.do_reconnect()
        self.assertTrue(status['success'])
        self.assertEqual([call.args[1] for call in request.call_args_list], ['/api/asf/restart', '/api/playstatus/play'])
        self.assertEqual(request.call_args_list[1].args[2]['Bot'], 'target bot')

    def test_play_failure_returns_false_without_legacy_cache_write(self):
        with patch.object(bridge, 'bot_info', return_value={'IsConnectedAndLoggedOn': True}), \
             patch.object(bridge, 'plugin_play_status', return_value=None), \
             patch.object(bridge, 'asf_request', return_value=(200, {'Success': False, 'Message': 'play failed'})) as request, \
             patch.object(bridge.time, 'sleep'):
            status = bridge.do_reconnect()
        self.assertFalse(status['success'])
        self.assertEqual([call.args[1] for call in request.call_args_list], ['/api/playstatus/play'])
        self.assertIn('play failed', ' '.join(status['notes']))

    def test_placeholder_config_rejected_without_disclosing_password(self):
        config = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(bridge, 'CONFIG_PATH', str(path)):
                with self.assertRaisesRegex(ValueError, 'ui_token'):
                    bridge.load_config()


if __name__ == '__main__':
    unittest.main()
