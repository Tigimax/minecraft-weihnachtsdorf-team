"""
Tests for Discord slash command autocomplete (player_autocomplete).
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from wd_identity.discord_bot import player_autocomplete, _get_autocomplete_users
import wd_identity.discord_bot as bot_module


class TestAutocomplete(unittest.TestCase):
    def setUp(self):
        bot_module._ac_cache_users = []
        bot_module._ac_cache_time = 0.0

    @patch("wd_identity.discord_bot.LDAPClient")
    def test_autocomplete_by_ign(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_admins_and_mods.return_value = (
            [{"uid": "admin_one", "minecraftName": "AdminBoss", "discordName": "admin_d", "discordId": "111"}],
            []
        )
        mock_ldap.get_players.return_value = [
            {"uid": "tubuu", "minecraftName": "GoodTimesWithTub", "discordName": "tubuu", "discordId": "222"}
        ]

        interaction = MagicMock()
        interaction.guild = None

        # Search by part of IGN
        choices = asyncio.run(player_autocomplete(interaction, "GoodTimes"))
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, "GoodTimesWithTub")
        self.assertIn("GoodTimesWithTub", choices[0].name)
        self.assertIn("@tubuu", choices[0].name)

    @patch("wd_identity.discord_bot.LDAPClient")
    def test_autocomplete_by_at_mention(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_admins_and_mods.return_value = ([], [])
        mock_ldap.get_players.return_value = [
            {"uid": "tubuu", "minecraftName": "GoodTimesWithTub", "discordName": "tubuu", "discordId": "222"}
        ]

        interaction = MagicMock()
        interaction.guild = None

        # Search with @ prefix
        choices = asyncio.run(player_autocomplete(interaction, "@tub"))
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, "@tubuu")
        self.assertIn("@tubuu", choices[0].name)

    @patch("wd_identity.discord_bot.LDAPClient")
    def test_autocomplete_unregistered_discord_member(self, mock_ldap_cls):
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.get_admins_and_mods.return_value = ([], [])
        mock_ldap.get_players.return_value = []

        # Mock guild member
        member = MagicMock()
        member.bot = False
        member.id = 9999
        member.name = "sneezingcookie"
        member.display_name = "Cookie"
        member.global_name = "SneezingCookie"
        member.nick = None

        bot_member = MagicMock()
        bot_member.bot = True
        bot_member.id = 1564751378436456468
        bot_member.name = "WHD Privileges"

        guild = MagicMock()
        guild.members = [bot_member, member]

        interaction = MagicMock()
        interaction.guild = guild

        # Search with @sneez
        choices = asyncio.run(player_autocomplete(interaction, "@sneez"))
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, "@sneezingcookie")
        self.assertIn("@sneezingcookie", choices[0].name)
        # Bot must never appear in choices
        self.assertFalse(any("Privileges" in c.name for c in choices))


if __name__ == "__main__":
    unittest.main()
