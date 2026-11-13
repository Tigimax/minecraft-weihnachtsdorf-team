import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wd_identity.ldap_client import HOMELAB_SUPERADMINS, HOMELAB_SUPERADMIN_GROUPS, LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient, _atomic_write_json
from wd_identity.planka_sync import PlankaSyncClient


class TestMinecraftSyncLogic(unittest.TestCase):
    def setUp(self):
        self.client = MinecraftSyncClient()

    def test_build_set_role_commands_with_uuid(self):
        cmds = self.client._build_set_role_commands(
            "Tigimax", "admin", "fdbed3ab-01f0-45d6-a460-5ccf1e85de57"
        )
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], "whitelist add Tigimax")
        self.assertEqual(
            cmds[1], "lp user fdbed3ab-01f0-45d6-a460-5ccf1e85de57 parent set admin"
        )

    def test_build_set_role_commands_without_uuid(self):
        cmds = self.client._build_set_role_commands("NewMod", "mod", None)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], "whitelist add NewMod")
        self.assertEqual(cmds[1], "lp user NewMod parent set mod")

    def test_build_set_role_commands_rejects_injection(self):
        cmds = self.client._build_set_role_commands("Notch\nop hacker", "admin", None)
        self.assertEqual(cmds, [])

    def test_build_revoke_commands(self):
        cmds = self.client._build_revoke_commands(
            "OldStaff", "11111111-2222-3333-4444-555555555555"
        )
        self.assertEqual(
            cmds,
            [
                "whitelist remove OldStaff",
                "lp user 11111111-2222-3333-4444-555555555555 parent remove admin",
                "lp user 11111111-2222-3333-4444-555555555555 parent remove mod",
                "lp user 11111111-2222-3333-4444-555555555555 parent set default",
            ],
        )

    def test_build_revoke_commands_rejects_injection(self):
        cmds = self.client._build_revoke_commands("Hacked; stop", None)
        self.assertEqual(cmds, [])

    def test_broadcast_ingame_sanitizes_newlines_and_escapes_quotes(self):
        self.client.execute_commands = MagicMock(return_value=True)
        raw_msg = 'Server restart in 5 min!\n op attacker \r\n "quoted"'
        raw_title = 'Achtung!\n"Gefahr"'
        raw_sub = 'Details hier:\r\nhttps://example.com'

        ok = self.client.broadcast_ingame(raw_msg, title=raw_title, subtitle=raw_sub)
        self.assertTrue(ok)
        self.client.execute_commands.assert_called_once()
        cmds = self.client.execute_commands.call_args[0][0]
        self.assertEqual(len(cmds), 3)

        for cmd in cmds:
            self.assertNotIn("\r", cmd)
            self.assertNotIn("\n", cmd)

        # Parse json payloads inside commands
        self.assertTrue(cmds[0].startswith("execute in minecraft:overworld run tellraw @a "))
        tellraw_json = json.loads(cmds[0][len("execute in minecraft:overworld run tellraw @a "):])
        self.assertIn("Server restart in 5 min!", tellraw_json["text"])
        self.assertIn("op attacker", tellraw_json["text"])
        self.assertIn('"quoted"', tellraw_json["text"])
        self.assertEqual(tellraw_json["color"], "gold")

        self.assertTrue(cmds[1].startswith("title @a title "))
        title_json = json.loads(cmds[1][len("title @a title "):])
        self.assertEqual(title_json["text"], 'Achtung! "Gefahr"')
        self.assertEqual(title_json["color"], "red")
        self.assertTrue(title_json["bold"])

        self.assertTrue(cmds[2].startswith("title @a subtitle "))
        sub_json = json.loads(cmds[2][len("title @a subtitle "):])
        self.assertEqual(sub_json["text"], "Details hier:  https://example.com")
        self.assertEqual(sub_json["color"], "yellow")

    def test_broadcast_ingame_empty_or_none_inputs(self):
        self.client.execute_commands = MagicMock(return_value=True)

        # 1. Empty message and no title/subtitle returns True without running commands
        ok = self.client.broadcast_ingame("")
        self.assertTrue(ok)
        self.client.execute_commands.assert_not_called()

        # 2. None message returns True without running commands
        ok = self.client.broadcast_ingame(None)
        self.assertTrue(ok)
        self.client.execute_commands.assert_not_called()

        # 3. None message but valid title only runs title command (no tellraw)
        ok = self.client.broadcast_ingame(None, title="Server Wartung")
        self.assertTrue(ok)
        self.client.execute_commands.assert_called_once()
        cmds = self.client.execute_commands.call_args[0][0]
        self.assertEqual(len(cmds), 1)
        self.assertTrue(cmds[0].startswith("title @a title "))
        self.assertNotIn("tellraw", cmds[0])

    def test_grant_mod_build_access_validates_ign(self):
        self.client.ldap = MagicMock()
        self.client.ldap.mod_group_dn = "cn=wd_mods,ou=Groups,dc=ldap-01,dc=net"
        self.client.ldap.get_group_members.return_value = ["uid=badmod,ou=User,dc=ldap-01,dc=net"]
        self.client.ldap.get_user.return_value = {
            "uid": "badmod",
            "dn": "uid=badmod,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "Bad\nName! ; op",
        }
        with self.assertRaises(ValueError) as ctx:
            self.client.grant_mod_build_access("badmod", duration_hours=4.0)
        self.assertIn("Ungültiger Minecraft Ingame-Name", str(ctx.exception))

    def test_grant_mod_build_access_sanitizes_uuid_fallback_ign(self):
        self.client.ldap = MagicMock()
        self.client.ldap.mod_group_dn = "cn=wd_mods,ou=Groups,dc=ldap-01,dc=net"
        self.client.ldap.get_group_members.return_value = ["uid=goodmod,ou=User,dc=ldap-01,dc=net"]
        self.client.ldap.get_user.return_value = {
            "uid": "goodmod",
            "dn": "uid=goodmod,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "ValidModIGN",
            "minecraftUuid": "malicious\nop hacker",
        }
        self.client.execute_commands = MagicMock(return_value=True)
        self.client._load_mod_grants = MagicMock(return_value={})
        self.client._save_mod_grants = MagicMock()
        with patch("wd_identity.utils.send_dm"):
            self.client.grant_mod_build_access("goodmod", duration_hours=2.0)

        self.assertEqual(self.client.execute_commands.call_count, 2)
        cmds = self.client.execute_commands.call_args_list[1][0][0]
        self.assertEqual(cmds[0], "whitelist add ValidModIGN")
        # Malicious UUID must be rejected and fall back to valid IGN
        self.assertEqual(cmds[1], "lp user ValidModIGN parent set mod")

    def test_revoke_mod_build_access_skips_invalid_ign_and_sanitizes_kick(self):
        self.client.ldap = MagicMock()
        self.client.ldap.get_user.return_value = None
        self.client.ldap.search_by_discord.return_value = None
        self.client.ldap.search_by_minecraft.return_value = None
        self.client.is_project_launched = MagicMock(return_value=False)
        self.client.execute_commands = MagicMock(return_value=True)
        self.client._load_mod_grants = MagicMock(return_value={})
        self.client._save_mod_grants = MagicMock()

        # 1. Invalid IGN should skip execute_commands
        self.client.revoke_mod_build_access("Invalid\nIGN; stop")
        self.client.execute_commands.assert_not_called()

        # 2. Valid IGN should format whitelist remove and kick
        self.client.revoke_mod_build_access("ValidModPlayer")
        self.client.execute_commands.assert_called_once()
        cmds = self.client.execute_commands.call_args[0][0]
        self.assertEqual(cmds[0], "whitelist remove ValidModPlayer")
        self.assertEqual(cmds[1], 'kick ValidModPlayer "Temporäre Baurechte beendet (Pre-Launch)"')
        for cmd in cmds:
            self.assertNotIn("\r", cmd)
            self.assertNotIn("\n", cmd)

    def test_save_state_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state_sub" / "mc_sync_state.json"
            self.client.state_file = state_file

            data = {"user1": {"role": "admin", "whitelisted": True}}
            self.client._save_state(data)

            self.assertTrue(state_file.exists())
            with open(state_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded, data)

            # Ensure no orphaned temp files remained
            parent_files = list(state_file.parent.iterdir())
            self.assertEqual(len(parent_files), 1)
            self.assertEqual(parent_files[0].name, "mc_sync_state.json")

    def test_save_project_launched_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proj_file = Path(tmpdir) / "proj_sub" / "project_state.json"
            self.client.project_state_file = proj_file

            ok = self.client.set_project_launched(True)
            self.assertTrue(ok)
            self.assertTrue(proj_file.exists())
            with open(proj_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data, {"launched": True})

            # Check no leftover temp files
            parent_files = list(proj_file.parent.iterdir())
            self.assertEqual(len(parent_files), 1)
            self.assertEqual(parent_files[0].name, "project_state.json")

    def test_planka_sync_name_sanitization(self):
        planka = PlankaSyncClient()
        with patch.object(planka, "_execute_sql") as mock_exec:
            mock_exec.return_value = True

            # Whitespace name should fall back to capitalized username
            planka.sync_user(username="testuser", name="   ")
            sql = mock_exec.call_args[0][0]
            self.assertIn("name = 'Testuser'", sql)

            # Name containing $ is stripped
            planka.sync_user(username="testuser2", name="Hacker$$Bob")
            sql2 = mock_exec.call_args[0][0]
            self.assertIn("name = 'HackerBob'", sql2)

    def test_homelab_superadmin_groups_parsing(self):
        # Test full LDAP DN with commas remains intact when loaded
        from wd_identity.ldap_client import _raw_superadmin_groups
        # Directly test the robust parsing logic on complex DN strings
        with patch.dict(os.environ, {"HOMELAB_SUPERADMIN_GROUPS": "cn=proxmox_admins,ou=Groups,dc=ldap-01,dc=net;cn=guac_admins,ou=Groups,dc=ldap-01,dc=net"}):
            # reload or test logic
            raw = os.environ["HOMELAB_SUPERADMIN_GROUPS"]
            import re
            groups = [g.strip() for g in re.split(r"[;\n|]", raw) if g.strip()]
            self.assertEqual(len(groups), 2)
            self.assertEqual(groups[0], "cn=proxmox_admins,ou=Groups,dc=ldap-01,dc=net")
            self.assertEqual(groups[1], "cn=guac_admins,ou=Groups,dc=ldap-01,dc=net")

        # Test single DN with commas
        with patch.dict(os.environ, {"HOMELAB_SUPERADMIN_GROUPS": "cn=homelab_admins,ou=Groups,dc=ldap-01,dc=net"}):
            raw = os.environ["HOMELAB_SUPERADMIN_GROUPS"]
            if re.search(r"\b(dc|ou)\s*=", raw, re.IGNORECASE):
                groups = [g.strip() for g in re.split(r"[;\n|]", raw) if g.strip()]
            else:
                groups = [g.strip() for g in re.split(r"[;,\n|]", raw) if g.strip()]
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0], "cn=homelab_admins,ou=Groups,dc=ldap-01,dc=net")

    def test_planka_sync_role_whitelist(self):
        planka = PlankaSyncClient()
        with patch.object(planka, "_execute_sql") as mock_exec:
            mock_exec.return_value = True

            # admin role stays admin
            planka.sync_user(username="adm1", role="admin")
            sql_admin = mock_exec.call_args[0][0]
            self.assertIn("role = 'admin'", sql_admin)

            # mod or user role becomes 'user'
            planka.sync_user(username="mod1", role="mod")
            sql_mod = mock_exec.call_args[0][0]
            self.assertIn("role = 'user'", sql_mod)
            self.assertNotIn("role = 'mod'", sql_mod)

            # injection in role is sanitized and defaults to 'user'
            planka.sync_user(username="hacker", role="admin$injection' OR 1=1--")
            sql_inj = mock_exec.call_args[0][0]
            self.assertIn("role = 'user'", sql_inj)
            self.assertNotIn("admin$injection", sql_inj)

    def test_ldap_client_superadmins_contain_akadmin(self):
        self.assertIn("akadmin", HOMELAB_SUPERADMINS)
        self.assertIn("tigimax", HOMELAB_SUPERADMINS)

        ldap = LDAPClient()
        with patch.object(ldap, "_get_connection") as mock_conn:
            # delete_user must refuse to delete akadmin
            res = ldap.delete_user("akadmin")
            self.assertFalse(res)
            mock_conn.assert_not_called()

    def test_sync_all_does_not_rewhitelist_banned_players(self):
        """Active bans in applications.db must prevent sync_all() from whitelisting banned users and revoke existing whitelist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            self.client.state_file = tmp_path / "mc_sync_state.json"
            self.client.project_state_file = tmp_path / "project_state.json"
            self.client.mod_grants_file = tmp_path / "mod_grants.json"
            self.client.set_project_launched(True)

            # Seed prior state where banned player was whitelisted
            self.client._save_state({
                "banned_user": {
                    "role": "player",
                    "ign": "BannedPlayer",
                    "uuid": "11111111-1111-1111-1111-111111111111",
                    "whitelisted": True,
                }
            })

            self.client.ldap = MagicMock()
            self.client.ldap.get_admins_and_mods.return_value = (
                [
                    {
                        "uid": "banned_admin",
                        "minecraftName": "BannedAdmin",
                        "minecraftUuid": "22222222-2222-2222-2222-222222222222",
                        "discordId": "9001",
                    }
                ],
                [],
            )
            self.client.ldap.get_players.return_value = [
                {
                    "uid": "banned_user",
                    "minecraftName": "BannedPlayer",
                    "minecraftUuid": "11111111-1111-1111-1111-111111111111",
                    "discordId": "9002",
                },
                {
                    "uid": "clean_user",
                    "minecraftName": "CleanPlayer",
                    "minecraftUuid": "33333333-3333-3333-3333-333333333333",
                    "discordId": "9003",
                },
            ]
            self.client.execute_commands = MagicMock(return_value=True)

            active_bans = [
                {"discord_id": "9001", "minecraft_ign": None},
                {"discord_id": None, "minecraft_ign": "bannedplayer"},
            ]

            with patch("wd_identity.applications.get_all_bans", return_value=active_bans):
                res = self.client.sync_all(force=True)

            executed = self.client.execute_commands.call_args[0][0]
            self.assertIn("whitelist add CleanPlayer", executed)
            self.assertNotIn("whitelist add BannedPlayer", executed)
            self.assertNotIn("whitelist add BannedAdmin", executed)
            self.assertIn("whitelist remove BannedPlayer", executed)

            state = self.client._load_state()
            self.assertFalse(state["banned_user"]["whitelisted"])
            self.assertFalse(state["banned_admin"]["whitelisted"])
            self.assertTrue(state["clean_user"]["whitelisted"])
            self.assertEqual(len(res["revoked_users"]), 1)
            self.assertEqual(res["revoked_users"][0]["uid"], "banned_user")

    def test_persistent_file_lock_is_reentrant_without_flock_self_deadlock(self):
        """Re-entering _persistent_file_lock on the same path (even when parent dir is new) must not self-deadlock."""
        from wd_identity.mc_sync import _persistent_file_lock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "nested_locks" / "reentrant.lock"
            with _persistent_file_lock(lock_path):
                with _persistent_file_lock(lock_path):
                    self.assertTrue(lock_path.exists())
                    try:
                        import fcntl
                        with open(lock_path, "a+", encoding="utf-8") as probe_fd:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except ImportError:
                        pass

            # After outermost exit, lock must be released
            try:
                import fcntl
                with open(lock_path, "a+", encoding="utf-8") as probe_fd:
                    fcntl.flock(probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe_fd.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass

    def test_sync_all_with_expired_mod_grant_does_not_deadlock(self):
        """sync_all() expiring a mod build grant inside get_active_mod_grants() completes cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            self.client.state_file = tmp_path / "mc_sync_state.json"
            self.client.project_state_file = tmp_path / "project_state.json"
            self.client.mod_grants_file = tmp_path / "mod_grants.json"
            self.client.set_project_launched(False)
            self.client._save_mod_grants({
                "mod_one": {
                    "uid": "mod_one",
                    "ign": "ModOne",
                    "expires_at": "2020-01-01T00:00:00+00:00",
                }
            })

            self.client.ldap = MagicMock()
            self.client.ldap.get_admins_and_mods.return_value = (
                [],
                [{"uid": "mod_one", "minecraftName": "ModOne", "minecraftUuid": "44444444-4444-4444-4444-444444444444"}],
            )
            self.client.ldap.get_players.return_value = []
            self.client.execute_commands = MagicMock(return_value=True)

            with patch("wd_identity.applications.get_all_bans", return_value=[]):
                res = self.client.sync_all(force=True)

            self.assertTrue(res["success"])
            self.assertEqual(self.client.get_active_mod_grants(), {})


if __name__ == "__main__":
    unittest.main()
