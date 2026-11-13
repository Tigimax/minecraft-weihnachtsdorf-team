import unittest
from wd_identity.utils import (
    escape_sql_literal,
    sanitize_email,
    sanitize_username,
    validate_minecraft_ign,
)


class TestUtils(unittest.TestCase):
    def test_sanitize_username_umlauts(self):
        self.assertEqual(sanitize_username("Jürgen"), "juergen")
        self.assertEqual(sanitize_username("Müller"), "mueller")
        self.assertEqual(sanitize_username("Bärbel"), "baerbel")
        self.assertEqual(sanitize_username("Weiß"), "weiss")

    def test_sanitize_username_emojis_and_discord_tags(self):
        self.assertEqual(sanitize_username("🎅Santa#1234"), "santa_1234")
        self.assertEqual(sanitize_username("Test User 99"), "test_user_99")
        self.assertEqual(sanitize_username("..User--Name.."), "user--name")
        self.assertEqual(sanitize_username(""), "user")
        self.assertEqual(sanitize_username("   "), "user")
        self.assertEqual(sanitize_username("✨🎁🎄"), "user")

    def test_sanitize_email(self):
        self.assertEqual(sanitize_email("juergen"), "juergen@tigimax.mooo.com")
        self.assertEqual(sanitize_email("Jürgen"), "juergen@tigimax.mooo.com")
        self.assertEqual(
            sanitize_email("user", raw_email="custom.email@domain.com"),
            "custom.email@domain.com",
        )
        # Non-ASCII raw email falls back to clean ascii email
        self.assertEqual(
            sanitize_email("juergen", raw_email="jürgen@domain.com"),
            "juergen@tigimax.mooo.com",
        )

    def test_validate_minecraft_ign_valid(self):
        self.assertTrue(validate_minecraft_ign("Notch"))
        self.assertTrue(validate_minecraft_ign("jeb_"))
        self.assertTrue(validate_minecraft_ign("Player1234"))
        self.assertTrue(validate_minecraft_ign("A" * 16))
        self.assertTrue(validate_minecraft_ign("x"))

    def test_validate_minecraft_ign_invalid(self):
        self.assertFalse(validate_minecraft_ign(""))
        self.assertFalse(validate_minecraft_ign("   "))
        self.assertFalse(validate_minecraft_ign("Player Name"))
        self.assertFalse(validate_minecraft_ign("Notch\nstop"))
        self.assertFalse(validate_minecraft_ign("Notch; op hacker"))
        self.assertFalse(validate_minecraft_ign("A" * 17))
        self.assertFalse(validate_minecraft_ign("User!"))
        self.assertFalse(validate_minecraft_ign("User$"))
        self.assertFalse(validate_minecraft_ign(None))

    def test_escape_sql_literal(self):
        self.assertEqual(escape_sql_literal("O'Connor"), "O''Connor")
        self.assertEqual(escape_sql_literal("normal"), "normal")
        self.assertEqual(escape_sql_literal("it's a 'test'"), "it''s a ''test''")
        self.assertEqual(escape_sql_literal(None), "")

    def test_playerlist_format_entry_resolves_discord_name(self):
        from wd_identity.playerlist import _format_entry

        # Case 1: ID present -> shows mention
        u1 = {"minecraftName": "Steve", "discordId": "123456789", "discordName": "SteveDiscord"}
        entry1 = _format_entry(u1, "player", is_live=False, member_map={})
        self.assertIn("<@123456789>", entry1)

        # Case 2: discordId resolved from member_map
        u2 = {"minecraftName": "Alex", "discordName": "alex"}
        entry2 = _format_entry(u2, "player", is_live=True, member_map={"alex": "987654321"})
        self.assertIn("<@987654321>", entry2)

        # Case 3: No discordId and not in member_map -> plain fallback to discordName
        u3 = {"minecraftName": "Herobrine", "discordName": "HeroUser"}
        entry3 = _format_entry(u3, "player", is_live=True, member_map={})
        self.assertIn("`@HeroUser`", entry3)

    def test_playerlist_chunk_lines_respects_discord_limit(self):
        from wd_identity.playerlist import _chunk_lines

        long_line = "• ⛄ **VeryLongPlayerName123** • <@123456789012345678>"
        lines = [long_line for _ in range(50)]
        chunks = _chunk_lines(lines, max_items=25, max_chars=1000)

        self.assertTrue(len(chunks) >= 2)
        for i, chunk in enumerate(chunks):
            chunk_text = "\n".join(chunk)
            self.assertLessEqual(len(chunk_text), 1024, f"Chunk {i} exceeded Discord limit: {len(chunk_text)}")
            self.assertLessEqual(len(chunk), 25)

    def test_playerlist_build_discord_embed_under_6000_chars_with_120_players(self):
        from unittest.mock import patch
        import discord
        from wd_identity import playerlist

        admins = [
            {
                "uid": f"admin{i}",
                "minecraftName": f"VeryLongAdminName{i:02d}",
                "discordName": f"AdminDisc{i}",
                "discordId": str(100000000000000000 + i),
            }
            for i in range(5)
        ]
        mods = [
            {
                "uid": f"mod{i}",
                "minecraftName": f"VeryLongModName_{i:02d}",
                "discordName": f"ModDisc{i}",
                "discordId": str(200000000000000000 + i),
            }
            for i in range(5)
        ]
        players = [
            {
                "uid": f"player{i:03d}",
                "minecraftName": f"PlayerLongIGN_{i:03d}",
                "discordName": f"PlayerDisc{i:03d}",
                "discordId": str(300000000000000000 + i),
            }
            for i in range(110)
        ]

        with patch("wd_identity.playerlist.LDAPClient") as mock_ldap_cls, \
             patch("wd_identity.playerlist.MinecraftSyncClient") as mock_mc_cls:
            mock_ldap = mock_ldap_cls.return_value
            mock_ldap.get_admins_and_mods.return_value = (admins, mods)
            mock_ldap.get_players.return_value = players
            mock_mc_cls.return_value.is_project_launched.return_value = True

            embed, content_hash = playerlist.build_discord_embed(member_map={})

        self.assertIsInstance(embed, discord.Embed)
        self.assertTrue(content_hash)
        self.assertLess(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)
        for field in embed.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), 1024)
        self.assertIn("120", embed.description)


if __name__ == "__main__":
    unittest.main()
