from unittest.mock import MagicMock, patch
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
        # Unlikely 16-char IGN that tests fallback
        name, p_uuid = get_player_profile("NonExistent9999")
        self.assertEqual(name, "NonExistent9999")
        self.assertTrue(len(p_uuid) == 36)

    def test_get_player_profile_invalid(self):
        # Invalid characters or length > 16 should raise ValueError
        with self.assertRaises(ValueError):
            get_player_profile("Notch\nstop")
        with self.assertRaises(ValueError):
            get_player_profile("TooLongPlayerName12345")
        with self.assertRaises(ValueError):
            get_player_profile("")

    @patch("requests.get")
    def test_fallback_is_not_cached(self, mock_get):
        get_player_profile.cache_clear()
        # Call 1: Mojang returns 429 rate limit
        resp_429 = MagicMock()
        resp_429.status_code = 429
        mock_get.return_value = resp_429

        name1, uuid1 = get_player_profile("TestPlayerA")
        self.assertEqual(name1, "TestPlayerA")
        self.assertEqual(uuid1, offline_player_uuid("TestPlayerA"))
        self.assertEqual(mock_get.call_count, 1)

        # Call 2: Mojang now recovers and returns 200
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"name": "TestPlayerA", "id": "11112222333344445555666677778888"}
        mock_get.return_value = resp_200

        name2, uuid2 = get_player_profile("TestPlayerA")
        self.assertEqual(mock_get.call_count, 2)  # Proves call 1 was NOT cached!
        self.assertEqual(name2, "TestPlayerA")
        self.assertEqual(uuid2, "11112222-3333-4444-5555-666677778888")

    @patch("requests.get")
    def test_success_is_cached(self, mock_get):
        get_player_profile.cache_clear()
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"name": "CachedPlayer", "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        mock_get.return_value = resp_200

        # Call 1
        name1, uuid1 = get_player_profile("CachedPlayer")
        self.assertEqual(uuid1, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(mock_get.call_count, 1)

        # Call 2 -> should hit LRU cache and NOT invoke requests.get again
        name2, uuid2 = get_player_profile("CachedPlayer")
        self.assertEqual(uuid2, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(mock_get.call_count, 1)

        # Call 3 with different casing -> should also hit cache
        name3, uuid3 = get_player_profile("cachedplayer")
        self.assertEqual(uuid3, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(mock_get.call_count, 1)

        # Cache clear allows re-fetch
        get_player_profile.cache_clear()
        name4, uuid4 = get_player_profile("CachedPlayer")
        self.assertEqual(mock_get.call_count, 2)

    @patch("requests.get")
    def test_require_existing_errors(self, mock_get):
        get_player_profile.cache_clear()

        # 404 -> ValueError
        resp_404 = MagicMock(status_code=404)
        mock_get.return_value = resp_404
        with self.assertRaises(ValueError):
            get_player_profile("NotFoundUser", require_existing=True)

        # 429 -> RuntimeError
        resp_429 = MagicMock(status_code=429)
        mock_get.return_value = resp_429
        with self.assertRaises(RuntimeError):
            get_player_profile("RateLimitedUser", require_existing=True)

        # 500 -> RuntimeError
        resp_500 = MagicMock(status_code=500)
        mock_get.return_value = resp_500
        with self.assertRaises(RuntimeError):
            get_player_profile("ServerErrorUser", require_existing=True)

        # Network error -> RuntimeError
        mock_get.side_effect = Exception("Connection refused")
        with self.assertRaises(RuntimeError):
            get_player_profile("NetworkErrorUser", require_existing=True)


if __name__ == "__main__":
    unittest.main()
