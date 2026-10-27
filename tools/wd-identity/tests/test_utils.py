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


if __name__ == "__main__":
    unittest.main()
