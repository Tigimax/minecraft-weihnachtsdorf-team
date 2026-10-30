"""
Unit tests for player MFA alternative (Discord bot code generation)
and strict TOTP requirement for staff members.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import subprocess

from wd_identity.authentik_client import AuthentikClient
from wd_identity.discord_bot import handle_auth_code_request
from wd_identity.webhook import MFADispatchRequest, mfa_auto_dispatch


class TestAuthentikClientMFA(unittest.TestCase):
    def setUp(self):
        self.ak = AuthentikClient()

    @patch.object(AuthentikClient, "get_user_by_username")
    @patch.object(AuthentikClient, "_execute_psql")
    def test_ensure_player_mfa_device_success(self, mock_psql, mock_get_user):
        mock_get_user.return_value = {"pk": 42, "username": "steve"}
        mock_psql.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        result = self.ak.ensure_player_mfa_device("steve")
        self.assertTrue(result)
        mock_psql.assert_called_once()
        self.assertEqual(mock_psql.call_args[0][1], {"uid": "42"})

    @patch.object(AuthentikClient, "get_user_by_username")
    @patch.object(AuthentikClient, "trigger_ldap_sync")
    def test_ensure_player_mfa_device_user_not_found(self, mock_sync, mock_get_user):
        mock_get_user.return_value = None

        result = self.ak.ensure_player_mfa_device("unknown_user")
        self.assertFalse(result)
        mock_sync.assert_called_once()

    @patch.object(AuthentikClient, "get_user_by_username")
    @patch.object(AuthentikClient, "_execute_psql")
    def test_has_totp_device_true(self, mock_psql, mock_get_user):
        mock_get_user.return_value = {"pk": 10, "username": "admin1"}
        mock_psql.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="1\n", stderr="")

        has_totp = self.ak.has_totp_device("admin1")
        self.assertTrue(has_totp)

    @patch.object(AuthentikClient, "get_user_by_username")
    @patch.object(AuthentikClient, "_execute_psql")
    def test_has_totp_device_false(self, mock_psql, mock_get_user):
        mock_get_user.return_value = {"pk": 10, "username": "player1"}
        mock_psql.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="0\n", stderr="")

        has_totp = self.ak.has_totp_device("player1")
        self.assertFalse(has_totp)

    @patch.object(AuthentikClient, "get_user_by_username")
    @patch.object(AuthentikClient, "ensure_player_mfa_device")
    @patch.object(AuthentikClient, "_execute_psql")
    def test_generate_static_token_for_user_success(self, mock_psql, mock_ensure, mock_get_user):
        mock_get_user.return_value = {"pk": 99, "username": "herobrine"}
        mock_ensure.return_value = True
        mock_psql.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        token = self.ak.generate_static_token_for_user("herobrine")
        self.assertIsNotNone(token)
        self.assertEqual(len(token), 6)
        self.assertTrue(token.isdigit())
        mock_ensure.assert_called_once_with("herobrine")
        mock_psql.assert_called_once()
        self.assertEqual(mock_psql.call_args[0][1]["uid"], "99")
        self.assertEqual(mock_psql.call_args[0][1]["tok"], token)


class TestDiscordBotAuthCode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.interaction = MagicMock()
        self.interaction.response.is_done.return_value = False
        self.interaction.response.defer = AsyncMock()
        self.interaction.followup.send = AsyncMock()
        self.interaction.user.id = 123456789
        self.interaction.user.name = "testuser_discord"

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_unlinked_user(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = None

        await handle_auth_code_request(self.interaction)

        self.interaction.response.defer.assert_called_once_with(ephemeral=True)
        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Kein Account verknüpft", msg)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_banned_user(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = {
            "uid": "banned_guy",
            "role": "player",
            "accountStatus": "banned",
        }

        await handle_auth_code_request(self.interaction)

        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Account gesperrt", msg)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_staff_admin_rejected(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = {
            "uid": "tigimax",
            "role": "admin",
            "accountStatus": "active",
        }

        await handle_auth_code_request(self.interaction)

        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Sicherheitshinweis für Teammitglieder", msg)
        self.assertIn("deaktiviert", msg)
        self.assertIn("Authenticator-App (TOTP)", msg)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_staff_mod_rejected(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = {
            "uid": "mod_user",
            "role": "mod",
            "accountStatus": "active",
        }

        await handle_auth_code_request(self.interaction)

        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Sicherheitshinweis für Teammitglieder", msg)
        self.assertIn("deaktiviert", msg)

    @patch("wd_identity.discord_bot.AuthentikClient")
    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_player_success(self, mock_ldap_cls, mock_ak_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_ldap.search_by_discord.return_value = {
            "uid": "super_player",
            "role": "player",
            "accountStatus": "active",
        }
        mock_ak.generate_static_token_for_user.return_value = "847291"

        await handle_auth_code_request(self.interaction)

        mock_ak.generate_static_token_for_user.assert_called_once_with("super_player")
        self.interaction.followup.send.assert_called_once()
        kwargs = self.interaction.followup.send.call_args[1]
        self.assertTrue(kwargs.get("ephemeral"))
        embed = kwargs.get("embed")
        self.assertIsNotNone(embed)
        self.assertEqual(embed.title, "🔑 Dein einmaliger Login-Code")
        self.assertIn("847291", embed.description)
        self.assertIn("super_player", embed.description)

    @patch("wd_identity.discord_bot.handle_auth_code_request")
    async def test_cmd_auth_code_delegates(self, mock_handler):
        from wd_identity.discord_bot import cmd_auth_code
        mock_handler.return_value = None
        await cmd_auth_code.callback(self.interaction)
        mock_handler.assert_called_once_with(self.interaction)

    @patch("wd_identity.discord_bot.handle_auth_code_request")
    async def test_cmd_code_delegates(self, mock_handler):
        from wd_identity.discord_bot import cmd_code
        mock_handler.return_value = None
        await cmd_code.callback(self.interaction)
        mock_handler.assert_called_once_with(self.interaction)


class TestWebhookMFAAutoDispatch(unittest.TestCase):
    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_mfa_auto_dispatch_with_req_discord_id(self, mock_ldap_cls, mock_ak_cls, mock_send_dm):
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_ldap.get_user.return_value = None  # user not yet in LDAP during enrollment
        mock_ak.has_totp_device.return_value = False
        mock_ak.generate_static_token_for_user.return_value = "123456"
        mock_send_dm.return_value = True

        req = MFADispatchRequest(username="newbie", discord_id="9988776655")
        res = mfa_auto_dispatch(req)

        self.assertEqual(res.get("status"), "success")
        self.assertTrue(res.get("dispatched"))
        mock_ak.generate_static_token_for_user.assert_called_once_with("newbie")
        mock_send_dm.assert_called_once()
        self.assertEqual(mock_send_dm.call_args[0][0], "9988776655")
        self.assertIn("123456", mock_send_dm.call_args[0][1])

    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_mfa_auto_dispatch_fallback_to_ak_attributes(self, mock_ldap_cls, mock_ak_cls, mock_send_dm):
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_ldap.get_user.return_value = None
        mock_ak.has_totp_device.return_value = False
        mock_ak.get_user_by_username.return_value = {
            "pk": 5,
            "username": "newbie",
            "attributes": {"discord_id": "4455667788"},
        }
        mock_ak.generate_static_token_for_user.return_value = "654321"
        mock_send_dm.return_value = True

        req = MFADispatchRequest(username="newbie")
        res = mfa_auto_dispatch(req)

        self.assertEqual(res.get("status"), "success")
        mock_send_dm.assert_called_once()
        self.assertEqual(mock_send_dm.call_args[0][0], "4455667788")


if __name__ == "__main__":
    unittest.main()
