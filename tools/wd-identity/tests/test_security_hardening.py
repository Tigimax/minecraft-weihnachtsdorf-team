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

import ssl
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from fastapi.testclient import TestClient

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.discord_bot import (
    handle_auth_code_request,
    is_admin_user,
)
from wd_identity.ldap_client import LDAPClient
from wd_identity.planka_sync import PlankaSyncClient
from wd_identity.webhook import (
    MFADispatchRequest,
    OnboardingRequest,
    app,
    mfa_auto_dispatch,
    process_onboarding,
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
        with patch("wd_identity.webhook.LDAPClient") as mock_ldap, \
             patch("wd_identity.webhook.AuthentikClient") as mock_ak:
            mock_ldap.return_value.set_password.return_value = True
            mock_ak.return_value.set_user_password.return_value = True
            mock_ak.return_value.get_user_by_username.return_value = {"attributes": {}}
            mock_ak.return_value.update_user_attributes.return_value = True
            res = self.client.post(
                "/api/web-setup/complete",
                json=payload,
                headers={"X-Webhook-Secret": "super-secret-token"},
            )
            self.assertEqual(res.status_code, 200)

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
    def test_webhook_logs_warning_when_secret_unset(self, mock_settings, mock_mc):
        """When WEBHOOK_SECRET is empty string, a security warning is logged upon receiving requests."""
        mock_settings.WEBHOOK_SECRET = ""
        with self.assertLogs("wd_identity.webhook", level="WARNING") as cm:
            client = TestClient(app)
            res = client.post("/api/sync", json={})
            self.assertEqual(res.status_code, 200)
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


if __name__ == "__main__":
    unittest.main()
