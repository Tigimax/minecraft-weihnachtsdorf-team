import unittest
from unittest.mock import MagicMock, patch
from click.testing import CliRunner

from wd_identity.cli import cli, resolve_notify_args


class TestNotifyCLI(unittest.TestCase):
    def test_resolve_notify_args_explicit_role(self):
        role, user, text = resolve_notify_args(
            target="Hallo Team", message=None, role_opt="admin", user_opt=None, message_opt=None
        )
        self.assertEqual(role, "admin")
        self.assertIsNone(user)
        self.assertEqual(text, "Hallo Team")

        role, user, text = resolve_notify_args(
            target=None, message=None, role_opt="mod", user_opt=None, message_opt="Nachricht"
        )
        self.assertEqual(role, "mod")
        self.assertIsNone(user)
        self.assertEqual(text, "Nachricht")

    def test_resolve_notify_args_explicit_user(self):
        role, user, text = resolve_notify_args(
            target="Hallo", message=None, role_opt=None, user_opt="nummernfeld", message_opt=None
        )
        self.assertIsNone(role)
        self.assertEqual(user, "nummernfeld")
        self.assertEqual(text, "Hallo")

        role, user, text = resolve_notify_args(
            target=None, message=None, role_opt=None, user_opt="nummernfeld", message_opt="Geheim"
        )
        self.assertIsNone(role)
        self.assertEqual(user, "nummernfeld")
        self.assertEqual(text, "Geheim")

    def test_resolve_notify_args_positional_role(self):
        role, user, text = resolve_notify_args(
            target="admins", message="Wartung heute", role_opt=None, user_opt=None, message_opt=None
        )
        self.assertEqual(role, "admin")
        self.assertIsNone(user)
        self.assertEqual(text, "Wartung heute")

        role, user, text = resolve_notify_args(
            target="staff", message="Alle versammeln", role_opt=None, user_opt=None, message_opt=None
        )
        self.assertEqual(role, "staff")
        self.assertIsNone(user)
        self.assertEqual(text, "Alle versammeln")

    def test_resolve_notify_args_positional_user(self):
        role, user, text = resolve_notify_args(
            target="tigimax", message="Dein Link", role_opt=None, user_opt=None, message_opt=None
        )
        self.assertIsNone(role)
        self.assertEqual(user, "tigimax")
        self.assertEqual(text, "Dein Link")

    @patch("wd_identity.cli.send_dm")
    @patch("wd_identity.cli.LDAPClient")
    def test_notify_single_user_found_in_ldap(self, mock_ldap_cls, mock_send_dm):
        mock_ldap = MagicMock()
        mock_ldap_cls.return_value = mock_ldap
        mock_ldap.search_by_discord.return_value = {
            "uid": "testuser",
            "discordName": "testuser_discord",
            "discordId": "12345678",
        }
        mock_send_dm.return_value = True

        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "testuser_discord", "Hallo Test"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Message successfully delivered", result.output)
        mock_send_dm.assert_called_once_with("12345678", "Hallo Test")

    @patch("wd_identity.cli.send_dm")
    @patch("wd_identity.cli.LDAPClient")
    def test_notify_single_user_not_in_ldap_direct_discord(self, mock_ldap_cls, mock_send_dm):
        mock_ldap = MagicMock()
        mock_ldap_cls.return_value = mock_ldap
        mock_ldap.search_by_discord.return_value = None
        mock_ldap.get_user.return_value = None
        mock_send_dm.return_value = True

        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "random_tag", "Direkte DM"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Message successfully delivered", result.output)
        mock_send_dm.assert_called_once_with("random_tag", "Direkte DM")

    @patch("wd_identity.cli.send_dm")
    @patch("wd_identity.cli.LDAPClient")
    def test_notify_role_broadcast(self, mock_ldap_cls, mock_send_dm):
        mock_ldap = MagicMock()
        mock_ldap_cls.return_value = mock_ldap
        mock_ldap.get_admins_and_mods.return_value = (
            [
                {"uid": "admin1", "discordName": "admin1_tag", "discordId": "111"},
                {"uid": "admin2", "discordName": "admin2_tag", "discordId": "222"},
            ],
            [],
        )
        mock_ldap.get_players.return_value = []
        mock_send_dm.return_value = True

        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--role", "admin", "--yes", "Git Repo Link: https://github.com/..."])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Broadcasting Discord notification to role 'admin'", result.output)
        self.assertIn("All 2 notifications successfully delivered", result.output)
        self.assertEqual(mock_send_dm.call_count, 2)
        mock_send_dm.assert_any_call("111", "Git Repo Link: https://github.com/...")
        mock_send_dm.assert_any_call("222", "Git Repo Link: https://github.com/...")

    def test_notify_missing_message_fails(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--role", "admin"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Please provide a message", result.output)

    def test_notify_missing_target_fails(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
