"""
Unit tests for complete account purging and master admin account protection.
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from wd_identity import applications
from wd_identity.config import settings
from wd_identity.membership import PROTECTED_INTERNAL_ACCOUNTS, purge_user_completely
from wd_identity.webhook import OnboardingRequest, process_onboarding


class TestPurgeAccount(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings.DATA_DIR = Path(self.temp_dir.name)
        applications.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_purge_user_history_sqlite(self):
        app_id = applications.create_application(
            discord_id="999888777",
            discord_username="test_applicant",
            app_type="initial",
            message="Test application",
        )
        self.assertGreater(app_id, 0)
        applications.record_ban(
            discord_id="999888777",
            minecraft_ign="TestIGN",
            banned_by="admin",
            reason="Test Ban",
        )

        status_before = applications.get_user_status("999888777")
        self.assertTrue(status_before["is_banned"])

        purged_count = applications.purge_user_history(discord_id="999888777")
        self.assertGreater(purged_count, 0)

        status_after = applications.get_user_status("999888777")
        self.assertFalse(status_after["is_banned"])
        self.assertFalse(status_after["has_pending"])
        self.assertFalse(status_after["is_rejected"])

    @patch("wd_identity.membership.LDAPClient")
    def test_purge_refuses_protected_accounts(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.side_effect = lambda u: {"uid": u} if u in PROTECTED_INTERNAL_ACCOUNTS else None

        for protected in ["tigimax", "akadmin"]:
            res = purge_user_completely(protected)
            self.assertFalse(res["success"])
            self.assertIn("geschützter", res["error"])

    @patch("wd_identity.membership.sync_playerlist_rest")
    @patch("wd_identity.membership.log_admin_event")
    @patch("wd_identity.membership.MinecraftSyncClient")
    @patch("wd_identity.membership.PlankaSyncClient")
    @patch("wd_identity.membership.AuthentikClient")
    @patch("wd_identity.membership.LDAPClient")
    def test_purge_regular_player_success(
        self,
        mock_ldap_cls,
        mock_ak_cls,
        mock_planka_cls,
        mock_mc_cls,
        mock_log,
        mock_playerlist,
    ):
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_planka = mock_planka_cls.return_value
        mock_mc = mock_mc_cls.return_value

        mock_ldap.get_user.return_value = {
            "uid": "test_player",
            "discordName": "TestPlayer#1234",
            "discordId": "123450001",
            "minecraftName": "TestIGN",
            "minecraftUuid": "uuid-1234",
            "mail": "test_player@tigimax.mooo.com",
        }
        mock_ldap.delete_user.return_value = True
        mock_ak.delete_user.return_value = True
        mock_planka.delete_user.return_value = True

        res = purge_user_completely("test_player", reason="Testing purge", initiator="Unit Test")

        self.assertTrue(res["success"])
        self.assertEqual(res["username"], "test_player")
        mock_ldap.delete_user.assert_called_once_with("test_player")
        mock_ak.delete_user.assert_called_once_with("test_player")
        mock_ak.delete_invitations.assert_called_once_with("test_player")
        mock_planka.delete_user.assert_called_once_with("test_player", email="test_player@tigimax.mooo.com")
        mock_mc.whitelist_remove.assert_called_once_with("TestIGN")
        mock_mc.revoke_player_privileges.assert_called_once_with("TestIGN", mc_uuid="uuid-1234")

    @patch("wd_identity.playerlist.sync_playerlist_rest")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_onboarding_maps_tigimax_to_tigimax_player(
        self,
        mock_ldap_cls,
        mock_ak_cls,
        mock_planka_cls,
        mock_mc_cls,
        mock_mojang,
        mock_playerlist,
    ):
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_mojang.return_value = ("Tigimax", "00000000-0000-0000-0000-000000000000")
        mock_ldap.get_user.return_value = None
        mock_ldap.search_by_discord.return_value = None

        req = OnboardingRequest(
            username="tigimax",
            minecraft_name="Tigimax",
            role="player",
            discord_name="tigimax",
            discord_id="330995438255603713",
        )

        res = process_onboarding(req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["username"], "tigimax_player")

        mock_ldap.create_or_update_user.assert_called_once()
        call_kwargs = mock_ldap.create_or_update_user.call_args[1]
        self.assertEqual(call_kwargs["username"], "tigimax_player")
        self.assertEqual(call_kwargs["role"], "player")
        mock_ak.add_user_to_group.assert_called_with("tigimax_player", "wd_players")


if __name__ == "__main__":
    unittest.main()
