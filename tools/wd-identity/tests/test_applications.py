"""
Unit tests for application and ban logic in applications.py.
"""

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from wd_identity import applications
from wd_identity.config import settings


class TestApplications(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings.DATA_DIR = Path(self.temp_dir.name)
        applications.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_get_application(self):
        app_id = applications.create_application(
            discord_id="123456789",
            discord_username="testuser",
            app_type="initial",
            message="Hallo ich würde gerne mitspielen!",
        )
        self.assertGreater(app_id, 0)

        app = applications.get_application(app_id)
        self.assertIsNotNone(app)
        self.assertEqual(app["discord_id"], "123456789")
        self.assertEqual(app["discord_username"], "testuser")
        self.assertEqual(app["status"], "pending")
        self.assertEqual(app["app_type"], "initial")

        status = applications.get_user_status("123456789")
        self.assertTrue(status["has_pending"])
        self.assertFalse(status["is_rejected"])
        self.assertFalse(status["in_cooldown"])

    def test_accept_application(self):
        app_id = applications.create_application(
            discord_id="111222333",
            discord_username="player1",
            app_type="initial",
            message="Bewerbung...",
        )
        updated = applications.accept_application(
            app_id=app_id,
            reviewer_id="999",
            reviewer_name="AdminTigi",
        )
        self.assertEqual(updated["status"], "accepted")
        self.assertEqual(updated["reviewer_name"], "AdminTigi")

        status = applications.get_user_status("111222333")
        self.assertFalse(status["has_pending"])
        self.assertFalse(status["is_rejected"])

    def test_reject_with_default_and_custom_cooldown(self):
        app_id = applications.create_application(
            discord_id="444555666",
            discord_username="griefer",
            app_type="initial",
            message="Lass mich rein",
        )
        # 1. Reject without cooldown
        updated = applications.reject_application(
            app_id=app_id,
            reviewer_id="999",
            reviewer_name="AdminTigi",
            reason="",
        )
        self.assertEqual(updated["status"], "rejected")
        self.assertIn("Leider können wir deine Bewerbung", updated["reason"])

        status = applications.get_user_status("444555666")
        self.assertFalse(status["has_pending"])
        self.assertTrue(status["is_rejected"])
        self.assertFalse(status["in_cooldown"])

        # 2. Submit unban appeal and reject with 7 days cooldown
        appeal_id = applications.create_application(
            discord_id="444555666",
            discord_username="griefer",
            app_type="unban",
            message="Entbannungsantrag bitte",
        )
        rejected_appeal = applications.reject_application(
            app_id=appeal_id,
            reviewer_id="999",
            reviewer_name="AdminTigi",
            reason="Zu früh für einen Antrag.",
            cooldown_days=7,
        )
        self.assertEqual(rejected_appeal["status"], "rejected")
        self.assertIsNotNone(rejected_appeal["cooldown_until"])

        status_cd = applications.get_user_status("444555666")
        self.assertTrue(status_cd["in_cooldown"])
        self.assertFalse(status_cd["is_permanent_cooldown"])
        self.assertIn("noch 6 Tag(e)" if "6" in (status_cd["cooldown_str"] or "") else "Tag", status_cd["cooldown_str"] or "")

        # 3. Permanent cooldown (-1)
        perm_app_id = applications.create_application(
            discord_id="999888777",
            discord_username="permabanned",
            app_type="unban",
            message="Gnade",
        )
        applications.reject_application(
            app_id=perm_app_id,
            reviewer_id="999",
            reviewer_name="AdminTigi",
            reason="Permanente Sperre.",
            cooldown_days=-1,
        )
        perm_status = applications.get_user_status("999888777")
        self.assertTrue(perm_status["in_cooldown"])
        self.assertTrue(perm_status["is_permanent_cooldown"])
        self.assertEqual(perm_status["cooldown_str"], "Dauerhaft (Permanent)")

    def test_bans_and_lift(self):
        ban_id = applications.record_ban(
            discord_id="777666555",
            minecraft_ign="BadGuy",
            banned_by="AdminTigi",
            reason="X-Ray",
        )
        self.assertGreater(ban_id, 0)

        status = applications.get_user_status("777666555")
        self.assertTrue(status["is_banned"])
        self.assertEqual(status["active_ban"]["reason"], "X-Ray")

        # Lift ban
        count = applications.lift_ban(discord_id="777666555")
        self.assertEqual(count, 1)

        status_after = applications.get_user_status("777666555")
        self.assertFalse(status_after["is_banned"])

    def test_connection_wal_and_busy_timeout(self):
        conn = applications.get_connection()
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        conn.close()
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, 30000)

    def test_init_db_caching(self):
        path_str = str(applications.get_db_path())
        self.assertIn(path_str, applications._initialized_dbs)

    def test_accept_application_clears_cooldown(self):
        # 1. User gets rejected with a cooldown
        app_id1 = applications.create_application(
            discord_id="888999000",
            discord_username="cooldown_user",
            app_type="initial",
            message="Initial app",
        )
        applications.reject_application(
            app_id=app_id1,
            reviewer_id="admin1",
            reviewer_name="Admin",
            reason="Try later",
            cooldown_days=14,
        )
        status_rej = applications.get_user_status("888999000")
        self.assertTrue(status_rej["in_cooldown"])

        # 2. Later admin creates/reviews an appeal or application and accepts it
        app_id2 = applications.create_application(
            discord_id="888999000",
            discord_username="cooldown_user",
            app_type="unban",
            message="Please unban",
        )
        accepted_app = applications.accept_application(
            app_id=app_id2,
            reviewer_id="admin1",
            reviewer_name="Admin",
        )
        self.assertIsNotNone(accepted_app)
        self.assertIsNone(accepted_app["cooldown_until"])

        # 3. Cooldown must be cleared completely
        status_acc = applications.get_user_status("888999000")
        self.assertFalse(status_acc["in_cooldown"])
        self.assertIsNone(status_acc["cooldown_until"])

    def test_purge_user_history_whitespace_ign_safe(self):
        # Create an app without ign (None)
        applications.create_application(
            discord_id="555444333",
            discord_username="no_ign_user",
            app_type="initial",
            message="hello",
            minecraft_ign=None,
        )
        # Purging with empty or whitespace IGN should not delete anything
        deleted = applications.purge_user_history(minecraft_ign="   ")
        self.assertEqual(deleted, 0)
        status = applications.get_user_status("555444333")
        self.assertTrue(status["has_pending"])

    def test_init_db_custom_path(self):
        custom_path = Path(self.temp_dir.name) / "custom_test.db"
        applications.init_db(db_path=custom_path)
        self.assertTrue(custom_path.exists())
        self.assertIn(str(custom_path), applications._initialized_dbs)

    def test_get_user_status_ignores_cooldown_on_non_rejected(self):
        # Insert a pending application with cooldown_until manually to simulate an odd DB state
        conn = applications.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO applications (discord_id, discord_username, app_type, message, status, cooldown_until)
                VALUES ('123999888', 'pending_user', 'initial', 'hi', 'pending', '2099-12-31 23:59:59')
                """
            )
        conn.close()

        status = applications.get_user_status("123999888")
        # Since status is 'pending' (not 'rejected'), cooldown_until should NOT trigger in_cooldown
        self.assertFalse(status["in_cooldown"])
        self.assertTrue(status["has_pending"])

    def test_accept_application_lifts_ign_only_ban(self):
        ban_id = applications.record_ban(
            discord_id=None,
            minecraft_ign="BannedGuy",
            banned_by="AdminTigi",
            reason="Griefing",
        )
        self.assertGreater(ban_id, 0)
        self.assertEqual(len(applications.get_all_bans(active_only=True)), 1)

        app_id = applications.create_application(
            discord_id="999000111",
            discord_username="banned_guy_dc",
            app_type="unban",
            message="Entschuldigung!",
            minecraft_ign="bannedguy",
        )
        accepted = applications.accept_application(
            app_id=app_id,
            reviewer_id="1",
            reviewer_name="AdminTigi",
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(applications.get_all_bans(active_only=True), [])
        self.assertFalse(applications.get_user_status("999000111")["is_banned"])


if __name__ == "__main__":
    unittest.main()
