import unittest
from unittest.mock import MagicMock
from wd_identity.mc_sync import MinecraftSyncClient


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


if __name__ == "__main__":
    unittest.main()
