"""
Unit tests for player MFA alternative (Discord bot code generation)
and strict TOTP requirement for staff members.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import subprocess

from wd_identity.authentik_client import AuthentikClient
from wd_identity.discord_bot import handle_auth_code_request
from wd_identity.ldap_client import LDAPClient
from wd_identity.webhook import (
    MFADispatchRequest,
    mfa_auto_dispatch,
    reset_mfa_cooldown_tracker,
)


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
        mock_ldap.search_by_discord_id.return_value = None
        mock_ldap.search_by_discord.return_value = None

        await handle_auth_code_request(self.interaction)

        self.interaction.response.defer.assert_called_once_with(ephemeral=True)
        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Kein Account verknüpft", msg)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_banned_user(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        user_data = {
            "uid": "banned_guy",
            "role": "player",
            "accountStatus": "banned",
        }
        mock_ldap.search_by_discord_id.return_value = user_data
        mock_ldap.search_by_discord.return_value = user_data

        await handle_auth_code_request(self.interaction)

        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Account gesperrt", msg)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_staff_admin_rejected(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        user_data = {
            "uid": "tigimax",
            "role": "admin",
            "accountStatus": "active",
        }
        mock_ldap.search_by_discord_id.return_value = user_data
        mock_ldap.search_by_discord.return_value = user_data

        await handle_auth_code_request(self.interaction)

        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args[0][0]
        self.assertIn("Sicherheitshinweis für Teammitglieder", msg)
        self.assertIn("deaktiviert", msg)
        self.assertIn("Authenticator-App (TOTP)", msg)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_staff_mod_rejected(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        user_data = {
            "uid": "mod_user",
            "role": "mod",
            "accountStatus": "active",
        }
        mock_ldap.search_by_discord_id.return_value = user_data
        mock_ldap.search_by_discord.return_value = user_data

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
        user_data = {
            "uid": "super_player",
            "role": "player",
            "accountStatus": "active",
        }
        mock_ldap.search_by_discord_id.return_value = user_data
        mock_ldap.search_by_discord.return_value = user_data
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
    def setUp(self):
        reset_mfa_cooldown_tracker()
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

    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_mfa_auto_dispatch_blocks_staff_resolved_from_ldap_role(
        self, mock_ldap_cls, mock_ak_cls, mock_send_dm
    ):
        """mfa_auto_dispatch blocks staff even if Authentik has no staff attributes/TOTP."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = {"pk": 12, "username": "mod_steve", "attributes": {}}
        mock_ak.has_totp_device.return_value = False
        mock_ldap.get_user.return_value = {
            "uid": "mod_steve",
            "dn": "uid=mod_steve,ou=User,dc=ldap-01,dc=net",
            "role": "mod",
            "discordId": "111222333",
        }

        req = MFADispatchRequest(username="mod_steve", discord_id="111222333")
        res = mfa_auto_dispatch(req)

        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "staff_or_totp")
        mock_ak.generate_static_token_for_user.assert_not_called()
        mock_send_dm.assert_not_called()

    @patch("wd_identity.webhook.applications.get_user_status")
    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_mfa_auto_dispatch_blocks_sqlite_banned_user_without_ldap_account_status(
        self, mock_ldap_cls, mock_ak_cls, mock_send_dm, mock_get_status
    ):
        """mfa_auto_dispatch blocks SQLite-banned user even when raw LDAP dict has no accountStatus."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = {"pk": 22, "username": "banned_steve", "attributes": {}}
        mock_ak.has_totp_device.return_value = False
        mock_ldap.get_user.return_value = {
            "uid": "banned_steve",
            "dn": "uid=banned_steve,ou=User,dc=ldap-01,dc=net",
            "role": "player",
            "discordId": "555666777",
        }
        mock_get_status.return_value = {"is_banned": True}

        req = MFADispatchRequest(username="banned_steve")
        res = mfa_auto_dispatch(req)

        self.assertEqual(res.get("status"), "error")
        self.assertIn("banned", res.get("detail", "").lower())
        mock_get_status.assert_called_once_with("555666777")
        mock_ak.generate_static_token_for_user.assert_not_called()
        mock_send_dm.assert_not_called()


class TestLDAPRoleResolutionAndBotGuards(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_entry(dn: str, attrs: dict):
        entry = MagicMock()
        entry.entry_dn = dn
        entry.entry_attributes_as_dict = attrs
        return entry

    @patch.object(LDAPClient, "_get_connection")
    def test_ldap_client_resolves_roles_from_groups_across_lookups(self, mock_get_conn):
        """LDAPClient populates 'role' from group memberships even when user entries lack a 'role' attr."""
        ldap = LDAPClient()
        admin_dn = "uid=adm,ou=User,dc=ldap-01,dc=net"
        mod_dn = "uid=mod1,ou=User,dc=ldap-01,dc=net"
        player_dn = "uid=ply1,ou=User,dc=ldap-01,dc=net"

        user_entries = [
            self._make_entry(admin_dn, {"uid": ["adm"], "discordId": ["101"], "minecraftName": ["AdminIGN"]}),
            self._make_entry(mod_dn, {"uid": ["mod1"], "discordId": ["102"], "minecraftName": ["ModIGN"]}),
            self._make_entry(player_dn, {"uid": ["ply1"], "discordId": ["103"], "minecraftName": ["PlayerIGN"]}),
        ]

        mock_conn = MagicMock()

        def fake_search(base, search_filter, attributes=None):
            if base == ldap.admin_group_dn:
                mock_conn.entries = [self._make_entry(ldap.admin_group_dn, {"member": [admin_dn]})]
            elif base == ldap.mod_group_dn:
                mock_conn.entries = [self._make_entry(ldap.mod_group_dn, {"member": [mod_dn]})]
            elif base == ldap.player_group_dn:
                mock_conn.entries = [self._make_entry(ldap.player_group_dn, {"member": [player_dn]})]
            elif "uid=adm" in search_filter:
                mock_conn.entries = [user_entries[0]]
            elif "discordId=102" in search_filter:
                mock_conn.entries = [user_entries[1]]
            elif "minecraftName=PlayerIGN" in search_filter:
                mock_conn.entries = [user_entries[2]]
            else:
                mock_conn.entries = user_entries

        mock_conn.search.side_effect = fake_search
        mock_get_conn.return_value = mock_conn

        self.assertEqual(ldap.get_user("adm")["role"], "admin")
        self.assertEqual(ldap.search_by_discord_id("102")["role"], "mod")
        self.assertEqual(ldap.search_by_minecraft("PlayerIGN")["role"], "player")

        all_users = {u["uid"]: u["role"] for u in ldap.list_all_users()}
        self.assertEqual(all_users, {"adm": "admin", "mod1": "mod", "ply1": "player"})

    @patch("wd_identity.discord_bot.AuthentikClient")
    @patch("wd_identity.discord_bot.applications.get_user_status")
    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_blocks_staff_without_role_attr_via_group_membership(
        self, mock_ldap_cls, mock_get_status, mock_ak_cls
    ):
        """handle_auth_code_request resolves staff role from LDAP groups when raw LDAP lacks 'role'."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord_id.return_value = {
            "uid": "stealth_mod",
            "dn": "uid=stealth_mod,ou=User,dc=ldap-01,dc=net",
        }
        mock_ldap.get_group_members.side_effect = lambda g: (
            ["uid=stealth_mod,ou=User,dc=ldap-01,dc=net"] if "wd_mods" in g else []
        )
        mock_get_status.return_value = {"is_banned": False}

        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.user.id = 222333444

        await handle_auth_code_request(interaction)

        msg = interaction.followup.send.call_args[0][0]
        self.assertIn("Sicherheitshinweis für Teammitglieder", msg)
        mock_ak_cls.return_value.generate_static_token_for_user.assert_not_called()

    @patch("wd_identity.discord_bot.AuthentikClient")
    @patch("wd_identity.discord_bot.applications.get_user_status")
    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_blocks_sqlite_banned_user_without_account_status(
        self, mock_ldap_cls, mock_get_status, mock_ak_cls
    ):
        """handle_auth_code_request blocks SQLite-banned user even when LDAP lacks 'accountStatus'."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord_id.return_value = {
            "uid": "banned_player",
            "dn": "uid=banned_player,ou=User,dc=ldap-01,dc=net",
            "role": "player",
        }
        mock_get_status.return_value = {"is_banned": True}

        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.user.id = 333444555

        await handle_auth_code_request(interaction)

        msg = interaction.followup.send.call_args[0][0]
        self.assertIn("Account gesperrt", msg)
        mock_ak_cls.return_value.generate_static_token_for_user.assert_not_called()

    async def test_slash_command_aliases_delegate_via_callback(self):
        """Slash command aliases must delegate via .callback without raising TypeError on Command."""
        from wd_identity import discord_bot

        dummy_choice = MagicMock()
        dummy_choice.value = "player"

        calls = [
            (discord_bot.cmd_spieler_bannen.callback, {"target": "Steve", "grund": "Test"}),
            (discord_bot.cmd_spieler_entbannen.callback, {"target": "Steve"}),
            (discord_bot.cmd_spieler_zuruecksetzen.callback, {"ziel": "Steve"}),
            (discord_bot.cmd_role_alias.callback, {"target": "Steve", "role": dummy_choice}),
            (discord_bot.cmd_mod_zugang_alias.callback, {"ziel": "ModSteve", "stunden": None, "entziehen": True}),
        ]

        with patch("wd_identity.discord_bot.is_admin_user", return_value=False):
            for fn, kwargs in calls:
                interaction = MagicMock()
                interaction.response.send_message = AsyncMock()
                await fn(interaction, **kwargs)
                interaction.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
