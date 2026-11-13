"""
Security and hardening tests for Weihnachtsdorf 7 Identity Management.
Tests cover:
1. Webhook authentication & authorization (secret / bearer token requirement, default host 127.0.0.1).
2. Webhook account hijacking prevention (no password overwrite on existing users).
3. Webhook role escalation & spoofing protection (mod/admin validation, MFA redirection prevention).
4. Strict Discord ID identity lookup without mutable Discord username fallback.
5. LDAP TLS certificate verification by default and configurable CA.
6. Authentik group synchronization on role changes (cleanup of old groups).
7. Planka user sync SQL safety (prioritizing username and preventing multi-row overwrite).
8. Discord Bot Admin check strict role ID verification (rejecting arbitrary role names).
"""

import asyncio
import ssl
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from fastapi.testclient import TestClient

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.discord_bot import (
    bot,
    bridge_on_message,
    cmd_spieler_kick,
    cmd_status,
    console_log_sync_loop,
    handle_admin_accept,
    handle_admin_appeal_accept,
    handle_auth_code_request,
    is_admin_user,
    on_member_remove,
    player_autocomplete,
)
from wd_identity.ldap_client import LDAPClient
from wd_identity.planka_sync import PlankaSyncClient
from wd_identity.webhook import (
    MFADispatchRequest,
    OnboardingRequest,
    app,
    mfa_auto_dispatch,
    process_onboarding,
    reset_web_setup_rate_limit,
)


class TestWebhookSecurityAndAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_default_webhook_host_is_localhost(self):
        """Webhook host must default to 127.0.0.1, not 0.0.0.0."""
        from wd_identity.config import Settings
        s = Settings()
        # Even if environment was 0.0.0.0, the default specification in code should be 127.0.0.1
        self.assertEqual(s.WEBHOOK_HOST, "127.0.0.1")

    @patch("wd_identity.webhook.settings")
    def test_webhook_endpoints_require_auth_when_secret_configured(self, mock_settings):
        mock_settings.WEBHOOK_SECRET = "super-secret-token"

        # Endpoints that must be protected:
        protected_endpoints = [
            ("POST", "/api/onboarding/complete", {"username": "test", "minecraft_name": "TestIGN"}),
            ("POST", "/api/mfa/auto-dispatch", {"username": "test"}),
            ("POST", "/api/sync", {}),
            ("POST", "/api/webhook/authentik", {"action": "test"}),
        ]

        for method, endpoint, payload in protected_endpoints:
            # 1. No auth header -> 401 or 403
            res = self.client.request(method, endpoint, json=payload)
            self.assertIn(
                res.status_code,
                (401, 403),
                f"Endpoint {endpoint} should reject request without auth secret",
            )

            # 2. Wrong auth header -> 401 or 403
            res = self.client.request(
                method,
                endpoint,
                json=payload,
                headers={"X-Webhook-Secret": "wrong-secret"},
            )
            self.assertIn(
                res.status_code,
                (401, 403),
                f"Endpoint {endpoint} should reject request with invalid secret",
            )

            # 3. Wrong Bearer token -> 401 or 403
            res = self.client.request(
                method,
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer wrong-secret"},
            )
            self.assertIn(
                res.status_code,
                (401, 403),
                f"Endpoint {endpoint} should reject request with invalid Bearer token",
            )

    @patch("wd_identity.webhook.settings")
    def test_web_setup_flow_verified_requires_auth_when_secret_configured(self, mock_settings):
        """complete_web_setup with code='flow_verified' must reject unauthorized callers without secret."""
        mock_settings.WEBHOOK_SECRET = "super-secret-token"
        payload = {
            "username": "victim",
            "password": "NewSecretPassword123!",
            "code": "flow_verified",
        }

        # 1. No auth header -> 401
        res = self.client.post("/api/web-setup/complete", json=payload)
        self.assertIn(res.status_code, (401, 403))

        # 2. Invalid secret -> 401
        res = self.client.post(
            "/api/web-setup/complete",
            json=payload,
            headers={"X-Webhook-Secret": "invalid-secret"},
        )
        self.assertIn(res.status_code, (401, 403))

        # 3. Valid secret -> proceeds to user update
        reset_web_setup_rate_limit()
        with patch("wd_identity.webhook.LDAPClient") as mock_ldap, \
             patch("wd_identity.webhook.AuthentikClient") as mock_ak:
            mock_ldap.return_value.set_password.return_value = True
            ak_instance = MagicMock(spec=AuthentikClient)
            ak_instance.set_user_password.return_value = True
            ak_instance.set_needs_web_setup.return_value = True
            mock_ak.return_value = ak_instance
            res = self.client.post(
                "/api/web-setup/complete",
                json=payload,
                headers={"X-Webhook-Secret": "super-secret-token"},
            )
            self.assertEqual(res.status_code, 200)
            ak_instance.set_user_password.assert_called_once_with("victim", "NewSecretPassword123!")
            ak_instance.set_needs_web_setup.assert_called_once_with("victim", False)

    @patch("wd_identity.webhook.settings")
    def test_public_status_endpoints_do_not_require_auth(self, mock_settings):
        mock_settings.WEBHOOK_SECRET = "super-secret-token"
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)

        with patch("wd_identity.webhook.MinecraftSyncClient") as mock_mc:
            mock_mc.return_value.is_project_launched.return_value = False
            res = self.client.get("/api/project/status")
            self.assertEqual(res.status_code, 200)

    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    def test_onboarding_cannot_overwrite_password_of_existing_user(
        self, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """Prevent account hijacking: if an LDAP user already exists, onboarding must not set password."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.return_value = {
            "uid": "existinguser",
            "role": "player",
            "discordId": "123456",
            "accountStatus": "active",
        }
        mock_mojang.return_value = ("ExistingIGN", "uuid-1234")

        req = OnboardingRequest(
            username="existinguser",
            minecraft_name="ExistingIGN",
            password="HackedPassword123!",
        )

        res = process_onboarding(req)
        self.assertEqual(res.get("status"), "success")
        # set_password MUST NOT be called!
        mock_ldap.set_password.assert_not_called()

    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    def test_onboarding_rejects_unverified_mod_role(
        self, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """A user cannot self-assign role='mod' without verified staff status."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.return_value = None  # New user
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = None
        mock_mojang.return_value = ("NewMod", "uuid-5678")

        req = OnboardingRequest(
            username="wannabe_mod",
            minecraft_name="NewMod",
            role="mod",
        )

        res = process_onboarding(req)
        # Role must be downgraded to 'player'
        self.assertEqual(res.get("role"), "player")

    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    def test_onboarding_rejects_admin_role_with_spoofed_discord_id(
        self, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """Attacker supplying an admin's Discord ID cannot escalate to admin without matching trusted identity."""
        mock_ldap = mock_ldap_cls.return_value
        # Existing user has player discord ID
        mock_ldap.get_user.return_value = {
            "uid": "attacker",
            "role": "player",
            "discordId": "999999999",  # Not an admin
        }
        mock_mojang.return_value = ("AttackerIGN", "uuid-9999")

        req = OnboardingRequest(
            username="attacker",
            minecraft_name="AttackerIGN",
            discord_id="909437200125538371",  # Admin ID spoofed
            role="admin",
        )

        res = process_onboarding(req)
        self.assertEqual(res.get("role"), "player")

    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_mfa_auto_dispatch_never_redirects_to_spoofed_discord_id(
        self, mock_ldap_cls, mock_ak_cls, mock_send_dm
    ):
        """
        If a user has a bound discordId in LDAP, mfa_auto_dispatch must send the token
        to the bound discordId and NEVER to a spoofed discord_id in the request payload.
        """
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value

        mock_ldap.get_user.return_value = {
            "uid": "legit_player",
            "role": "player",
            "accountStatus": "active",
            "discordId": "111222333444",  # True owner's Discord ID
        }
        mock_ak.has_totp_device.return_value = False
        mock_ak.generate_static_token_for_user.return_value = "777888"
        mock_send_dm.return_value = True

        # Attacker requests dispatch for legit_player with attacker's discord_id
        req = MFADispatchRequest(username="legit_player", discord_id="999000111")
        res = mfa_auto_dispatch(req)

        self.assertEqual(res.get("status"), "success")
        # Must have sent to 111222333444 (bound ID), NOT 999000111 (spoofed attacker ID)
        mock_send_dm.assert_called_once()
        self.assertEqual(mock_send_dm.call_args[0][0], "111222333444")


class TestIdentityLookupSafety(unittest.IsolatedAsyncioTestCase):
    @patch("wd_identity.ldap_client.LDAPClient._get_connection")
    def test_search_by_discord_id_strictly_matches_id(self, mock_get_conn):
        """search_by_discord_id must filter solely on discordId, never on discordName or uid."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        ldap = LDAPClient()
        ldap.search_by_discord_id("123456789")

        mock_conn.search.assert_called()
        search_filter = mock_conn.search.call_args[0][1]
        self.assertIn("discordId=123456789", search_filter)
        self.assertNotIn("discordName", search_filter)

    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_auth_code_never_falls_back_to_discord_username(self, mock_ldap_cls):
        """
        handle_auth_code_request must look up solely by discord_id and never by
        mutable discord username (interaction.user.name).
        """
        mock_ldap = mock_ldap_cls.return_value
        # Suppose search_by_discord_id returns None (user not found by ID)
        mock_ldap.search_by_discord_id.return_value = None
        # But search_by_discord or search by name might return an account if called!
        mock_ldap.search_by_discord.return_value = {"uid": "impersonated_victim"}

        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.user.id = 999999999
        interaction.user.name = "impersonated_victim"

        await handle_auth_code_request(interaction)

        # It must NOT issue a code for impersonated_victim!
        self.interaction_msg = interaction.followup.send.call_args[0][0]
        self.assertIn("Kein Account verknüpft", self.interaction_msg)
        mock_ldap.search_by_discord_id.assert_called_once_with("999999999")


class TestLDAPTLSSecurity(unittest.TestCase):
    @patch("wd_identity.ldap_client.Server")
    @patch("wd_identity.ldap_client.Connection")
    @patch("wd_identity.ldap_client.Tls")
    def test_ldap_enforces_tls_validation_by_default(self, mock_tls, mock_conn, mock_server):
        """Default LDAP client must require TLS certificate validation."""
        ldap = LDAPClient()
        # Ensure default setting is secure
        with patch.object(settings, "LDAP_TLS_VERIFY", True):
            with patch.object(settings, "LDAP_CA_CERT_FILE", ""):
                conn = ldap._get_connection()
                mock_tls.assert_called()
                kwargs = mock_tls.call_args[1]
                self.assertEqual(kwargs.get("validate"), ssl.CERT_REQUIRED)

    @patch("wd_identity.ldap_client.Server")
    @patch("wd_identity.ldap_client.Connection")
    @patch("wd_identity.ldap_client.Tls")
    def test_ldap_allows_insecure_only_if_explicitly_configured(self, mock_tls, mock_conn, mock_server):
        """Only when LDAP_TLS_VERIFY is explicitly False may CERT_NONE be used."""
        ldap = LDAPClient()
        with patch.object(settings, "LDAP_TLS_VERIFY", False):
            conn = ldap._get_connection()
            mock_tls.assert_called()
            kwargs = mock_tls.call_args[1]
            self.assertEqual(kwargs.get("validate"), ssl.CERT_NONE)


class TestAuthentikGroupSyncOnRoleChange(unittest.TestCase):
    @patch("requests.post")
    @patch("requests.get")
    def test_authentik_remove_user_from_group(self, mock_get, mock_post):
        ak = AuthentikClient()
        mock_get.side_effect = [
            # 1. get_user_by_username
            MagicMock(ok=True, json=lambda: {"results": [{"pk": 42, "username": "user1"}]}),
            # 2. get group by name
            MagicMock(ok=True, json=lambda: {"results": [{"pk": 100, "name": "wd_admins"}]}),
        ]
        mock_post.return_value = MagicMock(status_code=204)

        result = ak.remove_user_from_group("user1", "wd_admins")
        self.assertTrue(result)
        mock_post.assert_called_once()
        self.assertIn("/api/v3/core/groups/100/remove_user/", mock_post.call_args[0][0])
        self.assertEqual(mock_post.call_args[1]["json"], {"pk": 42})

    @patch.object(AuthentikClient, "remove_user_from_group")
    @patch.object(AuthentikClient, "add_user_to_group")
    def test_sync_user_groups_cleans_up_old_roles(self, mock_add, mock_remove):
        """When role changes to 'player', old roles ('wd_admins', 'wd_mods') must be removed."""
        ak = AuthentikClient()
        mock_add.return_value = True
        mock_remove.return_value = True

        result = ak.sync_user_groups("downgraded_admin", target_role="player")
        self.assertTrue(result)
        # Should have removed wd_admins and wd_mods
        removed_groups = {call.args[1] for call in mock_remove.call_args_list}
        self.assertEqual(removed_groups, {"wd_admins", "wd_mods"})
        # Should have added wd_players
        mock_add.assert_called_once_with("downgraded_admin", "wd_players")


class TestPlankaUserSyncSQLSafety(unittest.TestCase):
    @patch.object(PlankaSyncClient, "_execute_sql")
    def test_planka_sync_user_matches_username_first_without_multi_row_overwrite(self, mock_exec):
        client = PlankaSyncClient()
        mock_exec.return_value = True

        client.sync_user(username="testuser", email="test@example.com", name="Test User", role="admin")

        mock_exec.assert_called_once()
        sql = mock_exec.call_args[0][0]
        # SQL should NOT contain dangerous "WHERE username = '...' OR email = '...'" update
        # which can update multiple rows or overwrite another account's email
        self.assertNotIn("WHERE username = 'testuser' OR email = 'test@example.com';", sql)
        # It must prioritize username check
        self.assertIn("username = 'testuser'", sql)


class TestDiscordBotAdminPermissionStrictRole(unittest.TestCase):
    def test_arbitrary_role_named_admin_does_not_grant_bot_admin(self):
        """A user with a custom role named 'admin' (not having DISCORD_ADMIN_ROLE_ID) is rejected."""
        fake_user = MagicMock(spec=discord.Member)
        fake_user.guild_permissions.administrator = False

        # Role has name "admin", but ID does not match DISCORD_ADMIN_ROLE_ID
        fake_role = MagicMock()
        fake_role.id = 1122334455  # Custom role created by someone
        fake_role.name = "admin"
        fake_user.roles = [fake_role]

        with patch.object(settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371"):
            self.assertFalse(is_admin_user(fake_user))

    def test_user_with_configured_admin_role_id_is_accepted(self):
        fake_user = MagicMock(spec=discord.Member)
        fake_user.guild_permissions.administrator = False

        fake_role = MagicMock()
        fake_role.id = 909437200125538371
        fake_role.name = "wd-admin"
        fake_user.roles = [fake_role]

        with patch.object(settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371"):
            self.assertTrue(is_admin_user(fake_user))

    def test_user_with_guild_administrator_perm_is_accepted(self):
        fake_user = MagicMock(spec=discord.Member)
        fake_user.guild_permissions.administrator = True
        fake_user.roles = []

        self.assertTrue(is_admin_user(fake_user))


class TestAdvancedHardeningEdgeCases(unittest.IsolatedAsyncioTestCase):
    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.check_is_discord_admin")
    def test_new_user_cannot_escalate_to_admin_with_spoofed_discord_id(
        self, mock_check_admin, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """A new user without trusted invitation or LDAP record cannot claim admin even if providing an admin's Discord ID."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.return_value = None
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = None
        mock_mojang.return_value = ("AttackerIGN", "uuid-9999")
        mock_check_admin.return_value = (True, "Discord Administrator-Rolle")

        req = OnboardingRequest(
            username="new_attacker",
            minecraft_name="AttackerIGN",
            discord_id="909437200125538371",
            role="admin",
        )
        res = process_onboarding(req)
        self.assertEqual(res.get("role"), "player", "New user without trusted admin invite must be downgraded to player")

    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.check_is_discord_admin")
    def test_new_user_cannot_escalate_to_admin_with_spoofed_discord_name(
        self, mock_check_admin, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """A new user cannot escalate to admin by providing an admin's discord username/tag."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.return_value = None
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = None
        mock_mojang.return_value = ("AttackerIGN", "uuid-9999")
        mock_check_admin.return_value = (True, "Server-Eigentümer")

        req = OnboardingRequest(
            username="new_attacker",
            minecraft_name="AttackerIGN",
            discord_name="Tigimax",
            role="admin",
        )
        res = process_onboarding(req)
        self.assertEqual(res.get("role"), "player", "Spoofed discord_name must not grant admin")

    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.check_is_discord_mod")
    def test_new_user_cannot_escalate_to_mod_with_spoofed_discord_id(
        self, mock_check_mod, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """A new user cannot escalate to mod without trusted invitation or LDAP record."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.return_value = None
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = None
        mock_mojang.return_value = ("AttackerIGN", "uuid-9999")
        mock_check_mod.return_value = (True, "Discord Moderator-Rolle")

        req = OnboardingRequest(
            username="new_attacker",
            minecraft_name="AttackerIGN",
            discord_id="909437200125538371",
            role="mod",
        )
        res = process_onboarding(req)
        self.assertEqual(res.get("role"), "player", "New user without trusted mod invite must be downgraded to player")

    @patch("wd_identity.webhook.send_dm")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.LDAPClient")
    def test_mfa_auto_dispatch_existing_user_without_bound_discord_id_rejects_payload_id(
        self, mock_ldap_cls, mock_ak_cls, mock_send_dm
    ):
        """If user exists in LDAP but has no bound discordId, MFA token must NOT be sent to arbitrary payload discord_id."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ak = mock_ak_cls.return_value
        mock_ldap.get_user.return_value = {
            "uid": "legacy_user",
            "role": "player",
            "accountStatus": "active",
            "discordId": "",  # Unbound
        }
        mock_ak.get_user_by_username.return_value = {"attributes": {}}
        mock_ak.has_totp_device.return_value = False

        req = MFADispatchRequest(username="legacy_user", discord_id="999888777")
        res = mfa_auto_dispatch(req)
        self.assertEqual(res.get("status"), "error")
        mock_send_dm.assert_not_called()

    @patch("wd_identity.webhook.LDAPClient")
    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.PlankaSyncClient")
    @patch("wd_identity.webhook.AuthentikClient")
    @patch("wd_identity.webhook.get_player_profile")
    @patch("wd_identity.webhook.send_dm")
    def test_onboarding_rejects_discord_id_already_bound_to_different_user(
        self, mock_dm, mock_mojang, mock_ak_cls, mock_planka_cls, mock_mc_cls, mock_ldap_cls
    ):
        """Cannot bind a discord_id that is already bound to another LDAP user."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_user.return_value = None  # New user 'impostor'
        mock_ldap.search_by_discord_id.return_value = {
            "uid": "legit_victim",
            "discordId": "123456789",
        }
        mock_ak = mock_ak_cls.return_value
        mock_ak.get_user_by_username.return_value = None
        mock_mojang.return_value = ("ImpostorIGN", "uuid-1111")

        req = OnboardingRequest(
            username="impostor",
            minecraft_name="ImpostorIGN",
            discord_id="123456789",
            role="player",
        )
        res = process_onboarding(req)
        # In create_or_update_user call, discord_id must be cleared/None
        call_kwargs = mock_ldap.create_or_update_user.call_args[1]
        self.assertEqual(call_kwargs.get("discord_id"), "")

    def test_production_code_contains_no_magicmock_checks(self):
        """Production code must never check for MagicMock type in business logic."""
        import inspect
        from wd_identity import discord_bot, webhook, ldap_client
        for mod in (discord_bot, webhook, ldap_client):
            source = inspect.getsource(mod)
            self.assertNotIn("MagicMock", source, f"Module {mod.__name__} contains MagicMock reference in production code")

    @patch.object(PlankaSyncClient, "_execute_sql")
    def test_planka_sync_dollar_injection_safety(self, mock_exec):
        """Dollar characters in names must be stripped to prevent DO block delimiter injection."""
        client = PlankaSyncClient()
        mock_exec.return_value = True

        client.sync_user(username="injection_user", name="Evil$$Name", role="admin")
        mock_exec.assert_called_once()
        sql = mock_exec.call_args[0][0]
        self.assertIn("DO $planka_sync_user$", sql)
        self.assertNotIn("Evil$$Name", sql)
        self.assertIn("EvilName", sql)

    @patch("wd_identity.webhook.MinecraftSyncClient")
    @patch("wd_identity.webhook.settings")
    def test_webhook_rejects_when_secret_unset(self, mock_settings, mock_mc):
        """When WEBHOOK_SECRET is empty string, requests are rejected with HTTP 500 (fail-closed)."""
        mock_settings.WEBHOOK_SECRET = ""
        with self.assertLogs("wd_identity.webhook", level="ERROR") as cm:
            client = TestClient(app)
            res = client.post("/api/sync", json={})
            self.assertEqual(res.status_code, 500)
            self.assertEqual(res.json().get("detail"), "Webhook authentication secret is not configured.")
            self.assertTrue(any("WEBHOOK_SECRET is not configured" in record for record in cm.output))

    @patch("wd_identity.discord_bot.send_dm_rest")
    @patch("wd_identity.discord_bot.applications.lift_ban")
    @patch("wd_identity.discord_bot.AuthentikClient")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_appeal_accept_strictly_uses_search_by_discord_id(
        self, mock_ldap_cls, mock_mc_cls, mock_ak_cls, mock_lift_ban, mock_dm
    ):
        """handle_admin_appeal_accept must resolve user strictly by discord_id and never by discord_username."""
        from wd_identity.discord_bot import handle_admin_appeal_accept

        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord_id.return_value = {
            "uid": "appealed_user",
            "minecraftName": "AppealedIGN",
        }
        # If search_by_discord were called with mutable username, it would return someone else
        mock_ldap.search_by_discord.return_value = {"uid": "wrong_impersonated_user"}

        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.message.edit = AsyncMock()
        interaction.message.embeds = [MagicMock()]
        interaction.user.mention = "@admin"
        interaction.user.name = "admin"

        app_data = {
            "id": 12,
            "discord_id": "888777666",
            "discord_username": "mutable_impersonator",
            "status": "pending",
        }

        with patch("wd_identity.discord_bot.applications.get_application", return_value=app_data), \
             patch("wd_identity.discord_bot.applications.accept_application"), \
             patch("wd_identity.discord_bot.log_admin_event"), \
             patch("wd_identity.discord_bot.is_admin_user", return_value=True):
            await handle_admin_appeal_accept(interaction, 12)

        mock_ldap.search_by_discord_id.assert_called_once_with("888777666")
        mock_ldap.search_by_discord.assert_not_called()


class TestSecurityAuditMustFixHardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("wd_identity.webhook.settings")
    def test_must_fix_3_webhook_empty_secret_fails_closed_500(self, mock_settings):
        """Must-Fix #3: When WEBHOOK_SECRET is empty string, webhook auth fails closed with HTTP 500."""
        mock_settings.WEBHOOK_SECRET = ""
        endpoints = [
            ("POST", "/api/onboarding/complete", {"username": "test", "minecraft_name": "TestIGN"}),
            ("POST", "/api/mfa/auto-dispatch", {"username": "test"}),
            ("POST", "/api/sync", {}),
            ("POST", "/api/webhook/authentik", {"action": "test"}),
            ("POST", "/api/web-setup/complete", {"username": "test", "password": "ValidPassword123!", "code": "flow_verified"}),
        ]
        for method, url, payload in endpoints:
            res = self.client.request(method, url, json=payload)
            self.assertEqual(
                res.status_code,
                500,
                f"Endpoint {url} must return HTTP 500 when WEBHOOK_SECRET is unset",
            )
            self.assertEqual(
                res.json().get("detail"),
                "Webhook authentication secret is not configured.",
            )

    @patch("wd_identity.webhook.settings")
    def test_must_fix_3_complete_web_setup_enforces_min_8_char_password(self, mock_settings):
        """Must-Fix #3: complete_web_setup enforces password length >= 8."""
        mock_settings.WEBHOOK_SECRET = "test-secret"
        short_passwords = ["", "   ", "short", "1234567", "  12345 "]
        for pw in short_passwords:
            res = self.client.post(
                "/api/web-setup/complete",
                json={"username": "player1", "password": pw, "code": "flow_verified"},
                headers={"X-Webhook-Secret": "test-secret"},
            )
            self.assertEqual(res.status_code, 400, f"Password '{pw}' should be rejected with HTTP 400")
            self.assertIn("at least 8 characters", res.json().get("detail", ""))

    @patch("wd_identity.discord_bot.log_admin_event")
    @patch("wd_identity.discord_bot.revoke_user_privileges")
    async def test_must_fix_4_on_member_remove_strictly_uses_snowflake_id(self, mock_revoke, mock_log_admin):
        """Must-Fix #4: on_member_remove only revokes privileges by immutable snowflake ID."""
        mock_revoke.return_value = {"found": False}

        member = MagicMock(spec=discord.Member)
        member.bot = False
        member.id = 987654321012345678
        member.name = "AttackerImpersonator"
        member.display_name = "AdminNick"
        member.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        member.guild.name = "TestGuild"

        await on_member_remove(member)

        # Must be called once with snowflake ID string
        mock_revoke.assert_called_once_with(
            target="987654321012345678",
            reason="Discord-Server verlassen",
            initiator="Discord Gateway (Member Remove)",
        )
        # Verify no calls with member.name or member.display_name
        for call in mock_revoke.call_args_list:
            self.assertNotIn("AttackerImpersonator", call.kwargs.values())
            self.assertNotIn("AdminNick", call.kwargs.values())

    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.log_admin_event")
    async def test_must_fix_6_console_command_blacklist_bypass_blocked(self, mock_log_admin, mock_mc_cls):
        """Must-Fix #6: Console channel command blacklist normalizes and blocks bypassed syntax."""
        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        blocked_commands = [
            "/stop",
            "//stop",
            "///stop",
            "minecraft:stop",
            "   //minecraft:stop   ",
            "restart",
            "//restart",
            "op Hacker",
            "//op Hacker",
            "deop Admin",
            "ban-ip 1.2.3.4",
            "kill-all",
            "whitelist off",
            "//whitelist off",
            "/whitelist   off",
            "/execute as @a run op Hacker",
            "execute run deop Admin",
            "/execute as @a run lp user Steve permission set admin",
            "/luckperms user Steve permission set admin",
            "lp user Steve permission set admin",
            "/pardon-ip 1.2.3.4",
            "minecraft:execute as @a run minecraft:stop",
            "/execute run whitelist off",
            "/ /stop",
            "// /stop",
            "/// / // / stop",
            "minecraft:/stop",
            "///minecraft://stop",
            "/ /op Hacker",
            "custom_cmd run /op Hacker",
            "custom_cmd run //stop",
            "custom_cmd run minecraft:/op",
            "custom_cmd run /minecraft:op",
            "custom_cmd run //whitelist off",
            "alias run whitelist   off",
            "execute run //op Hacker",
            ":stop",
            "::stop",
            "minecraft:op Hacker",
            "/minecraft:op Hacker",
            "reload",
            "/reload",
            "minecraft:reload",
            "rl",
            "perm",
            "perms",
            "permission",
            "permissions",
            "luckperms:perm",
        ]

        console_channel_id = 999111222
        with patch.object(settings, "DISCORD_CHANNEL_CONSOLE_ID", str(console_channel_id)):
            for blocked_cmd in blocked_commands:
                mock_mc.execute_commands.reset_mock()
                msg = MagicMock(spec=discord.Message)
                msg.author = MagicMock(spec=discord.Member)
                msg.author.bot = False
                msg.author.guild_permissions.administrator = True
                msg.webhook_id = None
                msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
                msg.channel.id = console_channel_id
                msg.content = blocked_cmd
                msg.reply = AsyncMock()

                await bridge_on_message(msg)

                msg.reply.assert_called_once()
                self.assertIn("gesperrt", msg.reply.call_args[0][0])
                mock_mc.execute_commands.assert_not_called()

    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.log_admin_event")
    async def test_console_multiline_command_injection_blocked(self, mock_log_admin, mock_mc_cls):
        """Multi-line commands containing \\n or \\r must be rejected in #ingame-konsole."""
        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        console_channel_id = 999111222
        with patch.object(settings, "DISCORD_CHANNEL_CONSOLE_ID", str(console_channel_id)):
            for injected in ("say ok\nop Hacker", "say ok\r\nstop", "whitelist list\nreload"):
                mock_mc.execute_commands.reset_mock()
                msg = MagicMock(spec=discord.Message)
                msg.author = MagicMock(spec=discord.Member)
                msg.author.bot = False
                msg.author.guild_permissions.administrator = True
                msg.webhook_id = None
                msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
                msg.channel.id = console_channel_id
                msg.content = injected
                msg.reply = AsyncMock()

                await bridge_on_message(msg)

                msg.reply.assert_called_once()
                reply_text = msg.reply.call_args[0][0]
                self.assertIn("Mehrzeilige Befehle", reply_text)
                self.assertIn("gesperrt", reply_text)
                mock_mc.execute_commands.assert_not_called()

    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.log_admin_event")
    async def test_must_fix_6_console_command_allowed_commands_executed(self, mock_log_admin, mock_mc_cls):
        """Must-Fix #6: Safe console channel commands are stripped and executed."""
        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        console_channel_id = 999111222
        allowed_commands = [
            ("/whitelist list", "whitelist list"),
            ("say Server restart in 10 minutes", "say Server restart in 10 minutes"),
            ("/tps", "tps"),
            ("/ /whitelist list", "whitelist list"),
            ("// /tps", "tps"),
            ("  ///say hello world  ", "say hello world"),
        ]

        with patch.object(settings, "DISCORD_CHANNEL_CONSOLE_ID", str(console_channel_id)):
            for input_cmd, expected_cmd in allowed_commands:
                mock_mc.execute_commands.reset_mock()
                msg = MagicMock(spec=discord.Message)
                msg.author = MagicMock(spec=discord.Member)
                msg.author.bot = False
                msg.author.guild_permissions.administrator = True
                msg.webhook_id = None
                msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
                msg.channel.id = console_channel_id
                msg.content = input_cmd
                msg.reply = AsyncMock()

                await bridge_on_message(msg)

                msg.reply.assert_not_called()
                mock_mc.execute_commands.assert_called_once_with([expected_cmd])

    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    async def test_must_fix_11_cmd_spieler_kick_bot_target_uses_response_send_message(self, mock_admin):
        """Must-Fix #11: cmd_spieler_kick targeting a bot uses interaction.response.send_message before defer."""
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        # 1. Target bot.user.id via PropertyMock
        with patch.object(type(bot), "user", new_callable=unittest.mock.PropertyMock, return_value=MagicMock(id=1122334455)):
            await cmd_spieler_kick.callback(interaction, ziel="1122334455")
            interaction.response.send_message.assert_called_once()
            self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))
            interaction.followup.send.assert_not_called()
            interaction.response.defer.assert_not_called()

        # Also target hardcoded bot ID
        interaction.response.send_message.reset_mock()
        await cmd_spieler_kick.callback(interaction, ziel="1564751378436456468")
        interaction.response.send_message.assert_called_once()
        self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))
        interaction.followup.send.assert_not_called()
        interaction.response.defer.assert_not_called()

        # 2. Target another Discord bot member
        interaction.response.send_message.reset_mock()
        interaction.followup.send.reset_mock()
        interaction.response.defer.reset_mock()

        bot_member = MagicMock(spec=discord.Member)
        bot_member.bot = True
        bot_member.name = "TestBot"
        interaction.guild.get_member.return_value = bot_member

        await cmd_spieler_kick.callback(interaction, ziel="999888777")
        interaction.response.send_message.assert_called_once()
        self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))
        self.assertIn("ist ein Discord-Bot", interaction.response.send_message.call_args[0][0])
        interaction.followup.send.assert_not_called()
        interaction.response.defer.assert_not_called()

        # 3. Target bot member not cached in guild but found via bot.get_user
        interaction.response.send_message.reset_mock()
        interaction.followup.send.reset_mock()
        interaction.response.defer.reset_mock()
        interaction.guild.get_member.return_value = None

        uncached_bot = MagicMock(spec=discord.User)
        uncached_bot.bot = True
        uncached_bot.name = "UncachedBot"

        with patch.object(bot, "get_user", return_value=uncached_bot):
            await cmd_spieler_kick.callback(interaction, ziel="555444333")
            interaction.response.send_message.assert_called_once()
            self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))
            self.assertIn("ist ein Discord-Bot", interaction.response.send_message.call_args[0][0])
            interaction.followup.send.assert_not_called()
            interaction.response.defer.assert_not_called()

    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.LDAPClient")
    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    async def test_must_fix_6_cmd_spieler_kick_invalid_ign_rejected(self, mock_admin, mock_ldap_cls, mock_mc_cls):
        """Must-Fix #6: cmd_spieler_kick validates IGN with validate_minecraft_ign before defer."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = None
        mock_ldap.get_user.return_value = None
        mock_ldap.search_by_minecraft.return_value = None

        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.guild = None
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        # Injected IGN containing semicolon/newlines
        await cmd_spieler_kick.callback(interaction, ziel="Invalid;stop\n")

        interaction.response.send_message.assert_called_once()
        self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))
        self.assertIn("kein gültiger Minecraft Ingame-Name", interaction.response.send_message.call_args[0][0])
        interaction.response.defer.assert_not_called()
        mock_mc.execute_commands.assert_not_called()

    @patch("wd_identity.discord_bot.log_admin_event")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.LDAPClient")
    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    async def test_must_fix_6_cmd_spieler_kick_sanitizes_reason(self, mock_admin, mock_ldap_cls, mock_mc_cls, mock_log_admin):
        """Must-Fix #6: cmd_spieler_kick strips quotes and newlines from kick reason."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = None
        mock_ldap.get_user.return_value = {"minecraftName": "Steve"}

        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.name = "AdminUser"
        interaction.user.mention = "@AdminUser"
        interaction.guild = None
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        malicious_reason = 'Griefer" \r\n op Hacker \r\n "'
        await cmd_spieler_kick.callback(interaction, ziel="Steve", grund=malicious_reason)

        interaction.response.defer.assert_called_once()
        mock_mc.execute_commands.assert_called_once()
        sent_kick_cmd = mock_mc.execute_commands.call_args[0][0][0]
        self.assertNotIn('\r', sent_kick_cmd)
        self.assertNotIn('\n', sent_kick_cmd)
        self.assertEqual(sent_kick_cmd, 'kick Steve "Griefer  op Hacker  "')

    @patch("wd_identity.discord_bot.log_admin_event")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.LDAPClient")
    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    async def test_must_fix_13_cmd_spieler_kick_runs_ldap_in_thread(
        self, mock_admin, mock_ldap_cls, mock_mc_cls, mock_log_admin
    ):
        """Must-Fix #13: cmd_spieler_kick runs LDAP lookup asynchronously in worker thread before defer."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = None
        mock_ldap.get_user.return_value = {"minecraftName": "ValidPlayer"}

        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.name = "AdminUser"
        interaction.user.mention = "@AdminUser"
        interaction.guild = None
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        real_to_thread = asyncio.to_thread
        thread_calls = []

        async def spy_to_thread(func, /, *args, **kwargs):
            thread_calls.append(getattr(func, "__name__", str(func)))
            return await real_to_thread(func, *args, **kwargs)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            await cmd_spieler_kick.callback(interaction, ziel="ValidPlayer", grund="Kick test")

        self.assertIn("_resolve_ldap_user", thread_calls)
        interaction.response.defer.assert_called_once()
        mock_mc.execute_commands.assert_called_once_with(['kick ValidPlayer "Kick test"'])

    @patch("wd_identity.discord_bot.logger")
    @patch("wd_identity.discord_bot.settings")
    @patch("wd_identity.discord_bot.bot")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    async def test_must_fix_13_console_log_sync_loop_preserves_log_on_send_error(
        self, mock_mc_cls, mock_bot, mock_settings, mock_logger
    ):
        """Must-Fix #13: console_log_sync_loop does not advance _last_log when send fails, logs warning."""
        import wd_identity.discord_bot as bot_mod
        mock_settings.DISCORD_CHANNEL_CONSOLE_ID = "1234567890"
        channel = AsyncMock()
        channel.send.side_effect = RuntimeError("Discord API 500 Server Error")
        mock_bot.get_channel.return_value = channel

        mock_mc = mock_mc_cls.return_value
        mock_mc.get_log_lines.return_value = ["[12:00:00 INFO]: player joined", "[12:00:01 INFO]: player moved"]

        old_last_log = list(bot_mod._last_log)
        try:
            bot_mod._last_log = ["[12:00:00 INFO]: player joined"]
            # Run loop iteration
            await console_log_sync_loop.coro()

            # Verify send was attempted
            channel.send.assert_called_once()
            # Verify _last_log was NOT advanced to cur because send failed
            self.assertEqual(bot_mod._last_log, ["[12:00:00 INFO]: player joined"])
            # Verify exception was logged as warning
            mock_logger.warning.assert_called_with(
                "Error in console_log_sync_loop: Discord API 500 Server Error"
            )

            # Now verify subsequent iteration where send succeeds
            channel.send.reset_mock()
            channel.send.side_effect = None
            mock_logger.warning.reset_mock()

            await console_log_sync_loop.coro()
            channel.send.assert_called_once()
            # Now _last_log should be advanced
            self.assertEqual(bot_mod._last_log, ["[12:00:00 INFO]: player joined", "[12:00:01 INFO]: player moved"])
        finally:
            bot_mod._last_log = old_last_log

    @patch("wd_identity.discord_bot._get_autocomplete_users")
    async def test_must_fix_13_player_autocomplete_runs_in_thread(self, mock_get_users):
        """Must-Fix #13: player_autocomplete fetches users in worker thread."""
        mock_get_users.return_value = [
            {"uid": "testuser", "ign": "TestIGN", "discord_name": "testdisc", "discord_id": "12345", "role": "player"}
        ]
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = None

        real_to_thread = asyncio.to_thread
        thread_calls = []

        async def spy_to_thread(func, /, *args, **kwargs):
            thread_calls.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            choices = await player_autocomplete(interaction, "Test")

        self.assertIn(mock_get_users, thread_calls)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, "TestIGN")

    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    @patch("wd_identity.discord_bot.applications.get_application")
    @patch("wd_identity.discord_bot.get_player_profile", return_value=("MojangSteve", "uuid-1234"))
    @patch("wd_identity.discord_bot.LDAPClient")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.AuthentikClient")
    @patch("wd_identity.discord_bot.send_dm_rest", return_value=True)
    @patch("wd_identity.discord_bot.applications.accept_application")
    @patch("wd_identity.discord_bot.log_admin_event")
    async def test_must_fix_13_handle_admin_accept_runs_mojang_in_thread(
        self, mock_log, mock_upd, mock_dm, mock_auth, mock_mc, mock_ldap, mock_mojang, mock_get_app, mock_admin
    ):
        """Must-Fix #13: handle_admin_accept invokes get_player_profile via to_thread."""
        mock_get_app.return_value = {
            "id": 1,
            "status": "pending",
            "discord_id": "123456789",
            "discord_username": "steve",
            "minecraft_ign": "MojangSteve",
        }
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.message = None
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        real_to_thread = asyncio.to_thread
        thread_calls = []

        async def spy_to_thread(func, /, *args, **kwargs):
            thread_calls.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            await handle_admin_accept(interaction, app_id=1)

        self.assertIn(mock_mojang, thread_calls)

    @patch("wd_identity.discord_bot.get_player_profile", return_value=("PlayerName", "00000000-0000-0000-0000-000000000001"))
    @patch("wd_identity.discord_bot.LDAPClient")
    async def test_must_fix_13_cmd_status_runs_mojang_in_thread(self, mock_ldap_cls, mock_mojang):
        """Must-Fix #13: cmd_status resolves Mojang online UUID via to_thread when offline UUID detected."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_admins_and_mods.return_value = ([], [])
        mock_ldap.get_players.return_value = []
        mock_ldap.get_user.return_value = {
            "uid": "testplayer",
            "minecraftName": "TestPlayer",
            "minecraftUuid": "7642e72f-0000-0000-0000-000000000000",
            "discordName": "testplayer",
            "discordId": "12345",
        }
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.guild = None
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        real_to_thread = asyncio.to_thread
        thread_calls = []

        async def spy_to_thread(func, /, *args, **kwargs):
            thread_calls.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            await cmd_status.callback(interaction, spieler="testplayer")

        self.assertIn(mock_mojang, thread_calls)
        interaction.followup.send.assert_called_once()

    @patch("wd_identity.discord_bot.log_admin_event")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.LDAPClient")
    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    async def test_must_fix_13_cmd_spieler_kick_survives_ldap_exception(
        self, mock_admin, mock_ldap_cls, mock_mc_cls, mock_log_admin
    ):
        """Must-Fix #13: cmd_spieler_kick gracefully handles LDAP connection failure when target is valid IGN."""
        mock_ldap_cls.side_effect = RuntimeError("OpenLDAP connection timed out")

        mock_mc = mock_mc_cls.return_value
        mock_mc.execute_commands.return_value = True

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.name = "AdminUser"
        interaction.user.mention = "@AdminUser"
        interaction.guild = None
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await cmd_spieler_kick.callback(interaction, ziel="DirectPlayer", grund="Server maintenance")

        interaction.response.defer.assert_called_once()
        mock_mc.execute_commands.assert_called_once_with(['kick DirectPlayer "Server maintenance"'])
        interaction.followup.send.assert_called_once()

    @patch("wd_identity.discord_bot.is_admin_user", return_value=True)
    @patch("wd_identity.discord_bot.applications.get_application")
    @patch("wd_identity.discord_bot.get_player_profile", return_value=("MojangSteve", "uuid-1234"))
    @patch("wd_identity.discord_bot.LDAPClient")
    @patch("wd_identity.discord_bot.MinecraftSyncClient")
    @patch("wd_identity.discord_bot.AuthentikClient")
    @patch("wd_identity.discord_bot.send_dm_rest", return_value=True)
    @patch("wd_identity.discord_bot.applications.accept_application")
    @patch("wd_identity.discord_bot.log_admin_event")
    async def test_must_fix_13_handle_admin_appeal_accept_runs_mojang_in_thread(
        self, mock_log, mock_accept, mock_dm, mock_auth_cls, mock_mc_cls, mock_ldap_cls, mock_mojang, mock_get_app, mock_admin
    ):
        """Must-Fix #13: handle_admin_appeal_accept runs get_player_profile via to_thread for unprovisioned user."""
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord_id.return_value = None  # user has no account yet
        mock_get_app.return_value = {
            "id": 2,
            "status": "pending",
            "discord_id": "999111222",
            "discord_username": "appealuser",
            "minecraft_ign": "MojangSteve",
        }
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.name = "admin"
        interaction.user.mention = "@admin"
        interaction.message = MagicMock()
        interaction.message.embeds = [MagicMock()]
        interaction.message.edit = AsyncMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        real_to_thread = asyncio.to_thread
        thread_calls = []

        async def spy_to_thread(func, /, *args, **kwargs):
            thread_calls.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            await handle_admin_appeal_accept(interaction, app_id=2)

        self.assertIn(mock_mojang, thread_calls)


if __name__ == "__main__":
    unittest.main()
