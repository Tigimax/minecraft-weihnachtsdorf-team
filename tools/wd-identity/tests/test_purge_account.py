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

    @patch("wd_identity.membership.sync_playerlist_rest")
    @patch("wd_identity.membership.log_admin_event")
    @patch("wd_identity.membership.MinecraftSyncClient")
    @patch("wd_identity.membership.PlankaSyncClient")
    @patch("wd_identity.membership.AuthentikClient")
    @patch("wd_identity.membership.LDAPClient")
    def test_purge_order_satellites_before_ldap(
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

        order = []
        mock_ldap.get_user.return_value = {
            "uid": "order_player",
            "minecraftName": "OrderSteve",
            "minecraftUuid": "uuid-9999",
            "mail": "order@tigimax.mooo.com",
        }
        mock_mc.whitelist_remove.side_effect = lambda *a, **kw: order.append("minecraft")
        mock_planka.delete_user.side_effect = lambda *a, **kw: order.append("planka")
        mock_ak.delete_user.side_effect = lambda *a, **kw: order.append("authentik")
        mock_ldap.delete_user.side_effect = lambda *a, **kw: order.append("ldap")

        purge_user_completely("order_player")

        self.assertIn("minecraft", order)
        self.assertIn("planka", order)
        self.assertIn("authentik", order)
        self.assertIn("ldap", order)
        # All satellites must run before LDAP entry deletion
        self.assertLess(order.index("minecraft"), order.index("ldap"))
        self.assertLess(order.index("planka"), order.index("ldap"))
        self.assertLess(order.index("authentik"), order.index("ldap"))

    @patch("wd_identity.membership.sync_playerlist_rest")
    @patch("wd_identity.membership.log_admin_event")
    @patch("wd_identity.membership.MinecraftSyncClient")
    @patch("wd_identity.membership.PlankaSyncClient")
    @patch("wd_identity.membership.AuthentikClient")
    @patch("wd_identity.membership.LDAPClient")
    def test_purge_user_resolves_ign_from_application_and_sanitizes_reason(
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

        # User is NOT in LDAP
        mock_ldap.get_user.return_value = None
        mock_ldap.search_by_discord.return_value = None
        mock_ldap.search_by_minecraft.return_value = None

        # User has an application in SQLite
        app_id = applications.create_application(
            discord_id="999111222",
            discord_username="ApplicantSteve#5678",
            app_type="initial",
            message="Let me in",
            minecraft_ign="AppSteveIGN",
        )

        res = purge_user_completely(
            target="999111222",
            reason='User said "Bye"\r\nquit',
            initiator="AdminTest",
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["minecraft_name"], "AppSteveIGN")
        self.assertEqual(res["discord_id"], "999111222")

        # Minecraft kick command must have sanitized reason without quotes or newlines
        mock_mc.execute_commands.assert_called_once()
        cmd_list = mock_mc.execute_commands.call_args[0][0]
        kick_cmd = cmd_list[0]
        self.assertIn('kick AppSteveIGN "Account gelöscht: User said Bye  quit"', kick_cmd)
        self.assertIn("pardon AppSteveIGN", cmd_list[1])

        # Authentik should have cleaned up user and invitations
        mock_ak.delete_invitations.assert_called_once_with("applicantsteve5678")

        # DB records must be purged
        status = applications.get_user_status("999111222")
        self.assertFalse(status["has_pending"])
        self.assertIsNone(status["pending_app"])

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
