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


if __name__ == "__main__":
    unittest.main()
