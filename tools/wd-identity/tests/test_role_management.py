"""
Tests for role management (wd-admin role and set_user_role) and Moderator lifecycle.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from click.testing import CliRunner

from wd_identity.cli import cli
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.membership import set_user_role


def test_set_user_role_invalid_role():
    with pytest.raises(ValueError, match="Ungültige Rolle"):
        set_user_role("testuser", "invalid_role")


@patch("wd_identity.membership.LDAPClient")
def test_set_user_role_protected_account(mock_ldap_cls):
    mock_ldap = mock_ldap_cls.return_value
    mock_ldap.get_user.return_value = {
        "uid": "tigimax",
        "dn": "uid=tigimax,ou=users,dc=ldap-01,dc=net",
        "role": "admin",
    }
    with pytest.raises(PermissionError, match="geschützten System-Accounts"):
        set_user_role("tigimax", "player")


@patch("wd_identity.membership.check_is_discord_admin", return_value=(False, "Not an admin"))
@patch("wd_identity.membership.LDAPClient")
def test_set_user_role_admin_requires_discord_admin(mock_ldap_cls, mock_check_admin):
    mock_ldap = mock_ldap_cls.return_value
    mock_ldap.get_user.return_value = {
        "uid": "regular_user",
        "dn": "uid=regular_user,ou=users,dc=ldap-01,dc=net",
        "discordName": "regular_discord",
        "role": "player",
    }
    mock_ldap.get_group_members.return_value = []

    with pytest.raises(PermissionError, match="SICHERHEITSSPERRE"):
        set_user_role("regular_user", "admin", force=False)


@patch("wd_identity.membership.check_is_discord_mod", return_value=(False, "Not a mod"))
@patch("wd_identity.membership.LDAPClient")
def test_set_user_role_mod_requires_discord_mod(mock_ldap_cls, mock_check_mod):
    mock_ldap = mock_ldap_cls.return_value
    mock_ldap.get_user.return_value = {
        "uid": "regular_user",
        "dn": "uid=regular_user,ou=users,dc=ldap-01,dc=net",
        "discordName": "regular_discord",
        "role": "player",
    }
    mock_ldap.get_group_members.return_value = []

    with pytest.raises(PermissionError, match="SICHERHEITSSPERRE"):
        set_user_role("regular_user", "mod", force=False)


@patch("wd_identity.membership.check_is_discord_mod", return_value=(True, "Is mod"))
@patch("wd_identity.utils.send_dm", return_value=True)
@patch("wd_identity.membership.sync_playerlist_rest")
@patch("wd_identity.membership.log_admin_event")
@patch("wd_identity.membership.MinecraftSyncClient")
@patch("wd_identity.membership.AuthentikClient")
@patch("wd_identity.membership.LDAPClient")
def test_set_user_role_success_player_to_mod(
    mock_ldap_cls, mock_auth_cls, mock_mc_cls, mock_log, mock_sync, mock_send_dm, mock_check_mod
):
    mock_ldap = mock_ldap_cls.return_value
    mock_ldap.admin_group_dn = "cn=admin,ou=groups"
    mock_ldap.mod_group_dn = "cn=mod,ou=groups"
    mock_ldap.player_group_dn = "cn=player,ou=groups"

    mock_ldap.get_user.return_value = {
        "uid": "somedev",
        "dn": "uid=somedev,ou=users,dc=ldap-01,dc=net",
        "discordName": "somedev_dc",
        "minecraftName": "DevCraft",
        "minecraftUuid": "00000000-0000-0000-0000-000000000001",
    }
    mock_ldap.get_group_members.side_effect = lambda group: (
        ["uid=somedev,ou=users,dc=ldap-01,dc=net"] if group == "cn=player,ou=groups" else []
    )

    mock_auth = mock_auth_cls.return_value
    mock_mc = mock_mc_cls.return_value

    res = set_user_role("somedev", "mod", initiator="Tester")

    assert res["success"] is True
    assert res["old_role"] == "player"
    assert res["role"] == "mod"
    assert res["username"] == "somedev"

    # Verifies LDAP membership changes
    mock_ldap.add_member_to_group.assert_called_with("cn=mod,ou=groups", "uid=somedev,ou=users,dc=ldap-01,dc=net")
    mock_ldap.remove_member_from_group.assert_any_call("cn=admin,ou=groups", "uid=somedev,ou=users,dc=ldap-01,dc=net")
    mock_ldap.remove_member_from_group.assert_any_call("cn=player,ou=groups", "uid=somedev,ou=users,dc=ldap-01,dc=net")

    # Verifies Authentik attribute update & sync
    mock_auth.set_user_attributes.assert_called_with("somedev", {"role": "mod"})
    mock_auth.trigger_ldap_sync.assert_called_once()

    # Verifies Minecraft sync
    mock_mc.set_player_role.assert_called_with("DevCraft", "mod", mc_uuid="00000000-0000-0000-0000-000000000001")

    # Verifies mod welcome DM sent
    mock_send_dm.assert_called_once()
    dm_content = mock_send_dm.call_args[0][1]
    assert "Moderatoren-Team" in dm_content
    assert "WorldEdit" in dm_content
    assert "CoreProtect" in dm_content


@patch("wd_identity.membership.set_user_role")
def test_cli_role_command(mock_set_role):
    mock_set_role.return_value = {
        "success": True,
        "username": "tester",
        "old_role": "player",
        "role": "mod",
    }
    runner = CliRunner()
    result = runner.invoke(cli, ["role", "tester", "mod"])
    assert result.exit_code == 0
    assert "MOD" in result.output
    mock_set_role.assert_called_once_with("tester", "mod", initiator="wd-admin CLI", force=False)


@patch("wd_identity.mc_sync.LDAPClient")
def test_mod_build_grants_grant_and_revoke(mock_ldap_cls, tmp_path):
    mock_ldap = mock_ldap_cls.return_value
    mock_ldap.mod_group_dn = "cn=mod,ou=groups"
    mock_ldap.get_group_members.return_value = ["uid=testmod,ou=users"]
    mock_ldap.get_user.return_value = {
        "uid": "testmod",
        "dn": "uid=testmod,ou=users",
        "minecraftName": "TestModCraft",
        "minecraftUuid": "11111111-1111-1111-1111-111111111111",
        "discordName": "testmod_dc",
    }

    client = MinecraftSyncClient()
    client.mod_grants_file = tmp_path / "mod_grants.json"
    client.execute_commands = MagicMock(return_value=True)
    client.is_project_launched = MagicMock(return_value=False)

    # 1. Grant build access for 3 hours
    grant = client.grant_mod_build_access("testmod", duration_hours=3.0, initiator="AdminTester")
    assert grant["uid"] == "testmod"
    assert grant["duration_hours"] == 3.0
    assert client.has_active_mod_grant("testmod") is True

    # Check that whitelist add and LuckPerms group set mod were called
    calls = client.execute_commands.call_args_list
    assert any("whitelist add TestModCraft" in str(c) for c in calls)
    assert any("lp user 11111111-1111-1111-1111-111111111111 parent set mod" in str(c) for c in calls)

    # 2. Revoke build access
    rev = client.revoke_mod_build_access("testmod", initiator="AdminTester")
    assert rev["success"] is True
    assert rev["had_grant"] is True
    assert client.has_active_mod_grant("testmod") is False


@patch("wd_identity.mc_sync.LDAPClient")
def test_mod_pre_launch_sync_gating(mock_ldap_cls, tmp_path):
    mock_ldap = mock_ldap_cls.return_value
    mock_ldap.get_admins_and_mods.return_value = (
        [],
        [
            {
                "uid": "mod_with_grant",
                "dn": "uid=mod_with_grant,ou=users",
                "minecraftName": "GrantMod",
                "minecraftUuid": "22222222-2222-2222-2222-222222222222",
            },
            {
                "uid": "mod_without_grant",
                "dn": "uid=mod_without_grant,ou=users",
                "minecraftName": "NoGrantMod",
                "minecraftUuid": "33333333-3333-3333-3333-333333333333",
            },
        ],
    )
    mock_ldap.get_players.return_value = []

    client = MinecraftSyncClient()
    client.state_file = tmp_path / "sync_state.json"
    client.mod_grants_file = tmp_path / "mod_grants.json"
    client.execute_commands = MagicMock(return_value=True)
    client.is_project_launched = MagicMock(return_value=False)

    # Put active grant for mod_with_grant
    now = datetime.now(timezone.utc)
    client._save_mod_grants({
        "mod_with_grant": {
            "uid": "mod_with_grant",
            "ign": "GrantMod",
            "expires_at": (now + timedelta(hours=2)).isoformat(),
        }
    })

    res = client.sync_all(force=True)

    # mod_with_grant should be in synced_mods
    synced_igns = [m["ign"] for m in res["synced_mods"]]
    assert "GrantMod" in synced_igns

    # mod_without_grant should NOT be in synced_mods (it is in staged_players with role mod)
    staged_igns = [p["ign"] for p in res["staged_players"]]
    assert "NoGrantMod" in staged_igns
