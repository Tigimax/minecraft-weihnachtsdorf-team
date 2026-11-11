"""
Unit tests for BlueMap client IP filtering, bridge endpoints, and log rotation/sync helper.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wd_identity.bluemap_chat import client_ip_allowed, router
from wd_identity.mc_sync import MinecraftSyncClient, new_log_lines


class TestClientIpAllowed(unittest.TestCase):
    def _make_req(self, host):
        if host is None:
            return SimpleNamespace(client=None)
        return SimpleNamespace(client=SimpleNamespace(host=host))

    def test_exact_ip_match(self):
        req = self._make_req("192.168.1.149")
        self.assertTrue(client_ip_allowed(req, "127.0.0.1,::1,192.168.1.149"))

    def test_cidr_network_match(self):
        req = self._make_req("172.18.5.10")
        self.assertTrue(client_ip_allowed(req, "127.0.0.1,::1,172.16.0.0/12"))

    def test_empty_allowed_csv_allows_all(self):
        req = self._make_req("203.0.113.42")
        self.assertTrue(client_ip_allowed(req, ""))
        self.assertTrue(client_ip_allowed(req, "   "))

    def test_none_client_rejected(self):
        req = self._make_req(None)
        self.assertFalse(client_ip_allowed(req, "127.0.0.1,192.168.1.0/24"))

    def test_none_host_rejected(self):
        req = SimpleNamespace(client=SimpleNamespace(host=None))
        self.assertFalse(client_ip_allowed(req, "127.0.0.1,192.168.1.0/24"))

    def test_invalid_ip_rejected(self):
        req = self._make_req("not-an-ip")
        self.assertFalse(client_ip_allowed(req, "127.0.0.1,192.168.1.0/24"))

    def test_foreign_untrusted_ip_rejected(self):
        req = self._make_req("8.8.8.8")
        self.assertFalse(client_ip_allowed(req, "127.0.0.1,::1,192.168.1.149,172.16.0.0/12"))

    def test_ipv6_match(self):
        req = self._make_req("::1")
        self.assertTrue(client_ip_allowed(req, "127.0.0.1,::1,192.168.1.149"))


class TestNewLogLines(unittest.TestCase):
    def test_empty_prev_returns_empty(self):
        # Initial poll suppression: even if cur has lines, nothing should be back-posted
        prev = []
        cur = ["[12:00:00 INFO] Server started", "[12:00:01 INFO] Ready"]
        self.assertEqual(new_log_lines(prev, cur), [])

    def test_empty_cur_returns_empty(self):
        prev = ["[12:00:00 INFO] Line 1"]
        cur = []
        self.assertEqual(new_log_lines(prev, cur), [])

    def test_both_empty_returns_empty(self):
        self.assertEqual(new_log_lines([], []), [])

    def test_overlap_returns_new_suffix(self):
        prev = ["line 1", "line 2", "line 3"]
        cur = ["line 2", "line 3", "line 4", "line 5"]
        self.assertEqual(new_log_lines(prev, cur), ["line 4", "line 5"])

    def test_identical_returns_empty(self):
        prev = ["line 1", "line 2", "line 3"]
        cur = ["line 1", "line 2", "line 3"]
        self.assertEqual(new_log_lines(prev, cur), [])

    def test_complete_switch_returns_cur_completely(self):
        # When log rotated or completely changed (k == 0)
        prev = ["old log line 1", "old log line 2"]
        cur = ["restarted line 1", "restarted line 2"]
        self.assertEqual(new_log_lines(prev, cur), ["restarted line 1", "restarted line 2"])


class TestMinecraftSyncClientLogs(unittest.TestCase):
    @patch("requests.get")
    def test_get_log_lines_success(self, mock_get):
        client = MinecraftSyncClient()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"data": ["line1", "line2"]}
        mock_get.return_value = mock_resp

        lines = client.get_log_lines(size=50)
        self.assertEqual(lines, ["line1", "line2"])
        mock_get.assert_called_once()
        self.assertIn("size=50", mock_get.call_args[0][0])

    @patch("requests.get")
    def test_get_log_lines_error_handling(self, mock_get):
        client = MinecraftSyncClient()
        # Test HTTP non-200
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_get.return_value = mock_resp
        self.assertEqual(client.get_log_lines(), [])

        # Test request exception
        mock_get.side_effect = Exception("Connection refused")
        self.assertEqual(client.get_log_lines(), [])

    @patch.object(MinecraftSyncClient, "execute_commands")
    @patch.object(MinecraftSyncClient, "get_log_lines")
    def test_run_and_capture(self, mock_get_logs, mock_exec):
        client = MinecraftSyncClient()
        mock_get_logs.side_effect = [
            ["prev line 1", "prev line 2"],
            ["prev line 2", "new command output line"],
        ]
        mock_exec.return_value = True

        out = client.run_and_capture("list", wait=0.01)
        self.assertEqual(out, ["new command output line"])
        mock_exec.assert_called_once_with(["list"])

    @patch.object(MinecraftSyncClient, "execute_commands")
    @patch.object(MinecraftSyncClient, "get_log_lines")
    def test_run_and_capture_failure_raises_error(self, mock_get_logs, mock_exec):
        client = MinecraftSyncClient()
        mock_get_logs.return_value = ["line 1"]
        mock_exec.return_value = False

        with self.assertRaises(RuntimeError):
            client.run_and_capture("stop", wait=0.01)

    @patch.object(MinecraftSyncClient, "execute_commands")
    @patch.object(MinecraftSyncClient, "get_log_lines")
    def test_run_and_capture_empty_initial_log_captures_all(self, mock_get_logs, mock_exec):
        client = MinecraftSyncClient()
        mock_get_logs.side_effect = [
            [],
            ["command output after empty server log"],
        ]
        mock_exec.return_value = True

        out = client.run_and_capture("help", wait=0.01)
        self.assertEqual(out, ["command output after empty server log"])


class TestBlueMapEndpointsSecurityAndRouting(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.patcher = patch("wd_identity.chat_bridge.enqueue_to_discord", new_callable=AsyncMock)
        self.mock_enqueue = self.patcher.start()
        self.mock_bridge = MagicMock()
        self.mock_bridge.enqueue_to_discord = self.mock_enqueue

    def tearDown(self):
        self.patcher.stop()

    def test_ingame_endpoint_ip_blocked(self):
        client = TestClient(self.app, client=("8.8.8.8", 12345))
        resp = client.post("/api/bluemap/chat/ingame", json={"type": "chat", "username": "Steve", "message": "hi"})
        self.assertEqual(resp.status_code, 403)

    def test_ingame_endpoint_trusted_ip_enqueues_discord(self):
        client = TestClient(self.app, client=("192.168.1.149", 12345))
        payload = {"type": "chat", "username": "Steve", "message": "hello from mc", "source": "minecraft"}
        resp = client.post("/api/bluemap/chat/ingame", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.mock_bridge.enqueue_to_discord.assert_called_once()
        call_payload = self.mock_bridge.enqueue_to_discord.call_args[0][0]
        self.assertEqual(call_payload["username"], "Steve")
        self.assertEqual(call_payload["source"], "minecraft")

    def test_ingame_endpoint_discord_source_skips_enqueue(self):
        self.mock_bridge.enqueue_to_discord.reset_mock()
        client = TestClient(self.app, client=("192.168.1.149", 12345))
        payload = {"type": "chat", "username": "DiscordUser", "message": "echo", "source": "discord"}
        resp = client.post("/api/bluemap/chat/ingame", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.mock_bridge.enqueue_to_discord.assert_not_called()

    def test_webchat_send_untrusted_ip_blocked(self):
        client = TestClient(self.app, client=("8.8.8.8", 12345))
        resp = client.post("/addons/chat/world/send", json={"message": "hello"})
        self.assertEqual(resp.status_code, 403)

    def test_webchat_send_trusted_ip_enqueues_discord(self):
        self.mock_bridge.enqueue_to_discord.reset_mock()
        client = TestClient(self.app, client=("192.168.1.174", 12345))
        with patch("wd_identity.bluemap_chat.MinecraftSyncClient"), \
             patch("wd_identity.bluemap_chat.resolve_minecraft_player_from_authentik", return_value=("Steve", "uuid-123")):
            resp = client.post(
                "/addons/chat/world/send",
                json={"message": "hello from bluemap"},
                headers={"x-authentik-username": "steve"},
            )
            self.assertEqual(resp.status_code, 200)
            self.mock_bridge.enqueue_to_discord.assert_called_once_with({
                "type": "chat",
                "uuid": "uuid-123",
                "username": "Steve",
                "message": "hello from bluemap",
            })

    def test_integration_whoami_untrusted_ip_blocked(self):
        client = TestClient(self.app, client=("8.8.8.8", 12345))
        resp = client.get("/addons/integration/whoami")
        self.assertEqual(resp.status_code, 403)

    def test_integration_playerhead_untrusted_ip_blocked(self):
        client = TestClient(self.app, client=("8.8.8.8", 12345))
        resp = client.get("/addons/integration/playerhead/world")
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
