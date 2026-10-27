import unittest
from wd_identity.mojang_client import format_uuid, offline_player_uuid, get_player_profile


class TestMojangClient(unittest.TestCase):
    def test_format_uuid(self):
        raw = "fdbed3ab01f045d6a4605ccf1e85de57"
        formatted = format_uuid(raw)
        self.assertEqual(formatted, "fdbed3ab-01f0-45d6-a460-5ccf1e85de57")

    def test_format_uuid_already_formatted(self):
        formatted = "fdbed3ab-01f0-45d6-a460-5ccf1e85de57"
        self.assertEqual(format_uuid(formatted), formatted)

    def test_offline_player_uuid(self):
        u1 = offline_player_uuid("Tigimax")
        self.assertTrue(len(u1) == 36)
        self.assertEqual(u1[14], "3")  # UUID version 3
        # Offline UUID for same username is deterministic
        u2 = offline_player_uuid("Tigimax")
        self.assertEqual(u1, u2)

    def test_get_player_profile_real(self):
        # Tigimax is a known IGN
        name, p_uuid = get_player_profile("Tigimax")
        self.assertEqual(name.lower(), "tigimax")
        self.assertTrue(len(p_uuid) == 36)

    def test_get_player_profile_fallback(self):
        # Extremely unlikely IGN that tests fallback
        name, p_uuid = get_player_profile("NonExistentPlayerXYZ1234999")
        self.assertEqual(name, "NonExistentPlayerXYZ1234999")
        self.assertTrue(len(p_uuid) == 36)


if __name__ == "__main__":
    unittest.main()
