import unittest
from wd_identity.config import Settings


class TestConfig(unittest.TestCase):
    def test_settings_defaults(self):
        s = Settings()
        self.assertEqual(s.AUTHENTIK_BASE_URL, "https://auth.tigimax.mooo.com")
        self.assertEqual(s.AUTHENTIK_API_URL, "http://127.0.0.1:9000")
        self.assertEqual(s.WINGS_API_URL, "http://192.168.1.149:8080")
        self.assertEqual(s.WINGS_SERVER_UUID, "a173d02e-4efc-4f53-bc4f-4dd42f99c6f8")
        self.assertEqual(s.PLANKA_SSH_HOST, "192.168.1.174")
        self.assertEqual(s.PLANKA_CONTAINER, "planka-postgres-1")
        self.assertEqual(s.LDAP_ADMIN_GROUP, "cn=wd_admins,ou=Groups,dc=ldap-01,dc=net")
        self.assertEqual(s.LDAP_MOD_GROUP, "cn=wd_mods,ou=Groups,dc=ldap-01,dc=net")


if __name__ == "__main__":
    unittest.main()
