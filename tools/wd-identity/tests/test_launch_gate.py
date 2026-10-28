import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from wd_identity.mc_sync import MinecraftSyncClient


class TestLaunchGateAndPlayerSync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.project_state_file = Path(self.tmp_dir.name) / "project_state.json"
        self.mc_state_file = Path(self.tmp_dir.name) / "mc_sync_state.json"

        self.client = MinecraftSyncClient()
        self.client.project_state_file = self.project_state_file
        self.client.state_file = self.mc_state_file
        self.client.execute_commands = MagicMock(return_value=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_default_launch_state_is_false(self):
        self.assertFalse(self.client.is_project_launched())

    def test_set_project_launched_toggle(self):
        self.assertTrue(self.client.set_project_launched(True))
        self.assertTrue(self.client.is_project_launched())
        self.assertTrue(self.client.set_project_launched(False))
        self.assertFalse(self.client.is_project_launched())

    def test_set_player_role_pre_launch_skips_command(self):
        self.client.set_project_launched(False)
        result = self.client.set_player_role("NormalPlayer", "player", "1234-uuid")
        self.assertTrue(result)
        self.client.execute_commands.assert_not_called()

    def test_set_player_role_live_executes_whitelist(self):
        self.client.set_project_launched(True)
        result = self.client.set_player_role("NormalPlayer", "player", "1234-uuid")
        self.assertTrue(result)
        self.client.execute_commands.assert_called_once_with([
            "whitelist add NormalPlayer",
            "lp user 1234-uuid parent set default",
        ])

    def test_sync_all_pre_launch_stages_players_without_whitelist(self):
        self.client.set_project_launched(False)
        self.client.ldap.get_admins_and_mods = MagicMock(return_value=(
            [{"uid": "admin1", "minecraftName": "AdminIGN", "minecraftUuid": "uuid-admin"}],
            [],
        ))
        self.client.ldap.get_players = MagicMock(return_value=[
            {"uid": "player1", "minecraftName": "PlayerIGN", "minecraftUuid": "uuid-player"},
        ])

        report = self.client.sync_all(force=True)
        self.assertTrue(report["success"])
        self.assertFalse(report["is_launched"])
        self.assertEqual(len(report["synced_admins"]), 1)
        self.assertEqual(len(report["synced_players"]), 0)
        self.assertEqual(len(report["staged_players"]), 1)
        self.assertEqual(report["staged_players"][0]["ign"], "PlayerIGN")

        # Command check: ONLY admin commands sent
        cmds_called = self.client.execute_commands.call_args[0][0]
        self.assertIn("whitelist add AdminIGN", cmds_called)
        self.assertNotIn("whitelist add PlayerIGN", cmds_called)

    def test_sync_all_live_whitelists_players(self):
        self.client.set_project_launched(True)
        self.client.ldap.get_admins_and_mods = MagicMock(return_value=([], []))
        self.client.ldap.get_players = MagicMock(return_value=[
            {"uid": "player1", "minecraftName": "PlayerIGN", "minecraftUuid": "uuid-player"},
        ])

        report = self.client.sync_all(force=True)
        self.assertTrue(report["success"])
        self.assertTrue(report["is_launched"])
        self.assertEqual(len(report["synced_players"]), 1)
        self.assertEqual(len(report["staged_players"]), 0)

        cmds_called = self.client.execute_commands.call_args[0][0]
        self.assertIn("whitelist add PlayerIGN", cmds_called)
        self.assertIn("lp user uuid-player parent set default", cmds_called)

    def test_lock_project_removes_whitelisted_players(self):
        # 1. First launch and sync player
        self.client.set_project_launched(True)
        self.client.ldap.get_admins_and_mods = MagicMock(return_value=(
            [{"uid": "admin1", "minecraftName": "AdminIGN", "minecraftUuid": "uuid-admin"}],
            [],
        ))
        self.client.ldap.get_players = MagicMock(return_value=[
            {"uid": "player1", "minecraftName": "PlayerIGN", "minecraftUuid": "uuid-player"},
        ])
        self.client.sync_all(force=True)

        # 2. Now lock project
        report = self.client.lock_project()
        self.assertTrue(report["success"])
        self.assertFalse(report["is_launched"])
        self.assertEqual(len(report["revoked_users"]), 1)
        self.assertEqual(report["revoked_users"][0]["ign"], "PlayerIGN")

        cmds_called = self.client.execute_commands.call_args[0][0]
        self.assertIn("whitelist remove PlayerIGN", cmds_called)
        self.assertIn("whitelist add AdminIGN", cmds_called)


if __name__ == "__main__":
    unittest.main()
