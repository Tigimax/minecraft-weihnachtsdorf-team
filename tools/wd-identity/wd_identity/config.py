"""
Configuration module for wd-identity.
Loads configuration from environment variables and config files.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Try loading from standard configuration paths
config_paths = [
    Path("/etc/default/wd-identity"),
    Path("/opt/wd-identity/.env"),
    Path(".env"),
]

for p in config_paths:
    if p.exists():
        load_dotenv(p)


class Settings:
    # OpenLDAP Settings
    LDAP_URI: str = os.getenv("LDAP_URI", "ldaps://192.168.1.164:636")
    LDAP_BIND_DN: str = os.getenv(
        "LDAP_BIND_DN",
        "uid=svc-authentik,ou=ServiceAccounts,dc=ldap-01,dc=net",
    )
    LDAP_BIND_PASSWORD: str = os.getenv(
        "LDAP_BIND_PASSWORD",
        "",
    )
    LDAP_BASE_DN: str = os.getenv("LDAP_BASE_DN", "dc=ldap-01,dc=net")
    LDAP_USER_OU: str = os.getenv("LDAP_USER_OU", "ou=User,dc=ldap-01,dc=net")
    LDAP_GROUP_OU: str = os.getenv("LDAP_GROUP_OU", "ou=Groups,dc=ldap-01,dc=net")
    LDAP_ADMIN_GROUP: str = os.getenv(
        "LDAP_ADMIN_GROUP",
        "cn=wd_admins,ou=Groups,dc=ldap-01,dc=net",
    )
    LDAP_MOD_GROUP: str = os.getenv(
        "LDAP_MOD_GROUP",
        "cn=wd_mods,ou=Groups,dc=ldap-01,dc=net",
    )
    LDAP_PLAYER_GROUP: str = os.getenv(
        "LDAP_PLAYER_GROUP",
        "cn=wd_players,ou=Groups,dc=ldap-01,dc=net",
    )

    # Project Launch State File
    PROJECT_STATE_FILE: Path = Path(
        os.getenv("PROJECT_STATE_FILE", "/var/lib/wd-identity/project_state.json")
    )

    # Authentik Settings
    AUTHENTIK_BASE_URL: str = os.getenv(
        "AUTHENTIK_BASE_URL", "https://auth.tigimax.mooo.com"
    ).rstrip("/")
    AUTHENTIK_API_URL: str = os.getenv(
        "AUTHENTIK_API_URL", "http://127.0.0.1:9000"
    ).rstrip("/")
    AUTHENTIK_TOKEN: str = os.getenv(
        "AUTHENTIK_TOKEN", ""
    )
    AUTHENTIK_INVITATION_FLOW_UUID: str = os.getenv(
        "AUTHENTIK_INVITATION_FLOW_UUID", "9c936c43-2dad-40d9-a980-7c3054a5c414"
    )
    AUTHENTIK_INVITATION_FLOW_SLUG: str = os.getenv(
        "AUTHENTIK_INVITATION_FLOW_SLUG", "invitation-enrollment-flow"
    )
    AUTHENTIK_RECOVERY_FLOW_SLUG: str = os.getenv(
        "AUTHENTIK_RECOVERY_FLOW_SLUG", "default-recovery-flow"
    )

    # Minecraft Server (Pterodactyl Wings Node 2)
    WINGS_API_URL: str = os.getenv(
        "WINGS_API_URL", "http://192.168.1.149:8080"
    ).rstrip("/")
    WINGS_SERVER_UUID: str = os.getenv(
        "WINGS_SERVER_UUID", "a173d02e-4efc-4f53-bc4f-4dd42f99c6f8"
    )
    WINGS_BEARER_TOKEN: str = os.getenv(
        "WINGS_BEARER_TOKEN",
        "",
    )

    # Planka Integration (Postgres via SSH on usrv01)
    PLANKA_SSH_HOST: str = os.getenv("PLANKA_SSH_HOST", "192.168.1.174")
    PLANKA_SSH_USER: str = os.getenv("PLANKA_SSH_USER", "tigimax")
    PLANKA_CONTAINER: str = os.getenv("PLANKA_CONTAINER", "planka-postgres-1")
    PLANKA_DB_NAME: str = os.getenv("PLANKA_DB_NAME", "planka")
    PLANKA_DB_USER: str = os.getenv("PLANKA_DB_USER", "planka")

    # Discord Bot Settings
    DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
    DISCORD_GUILD_ID: str = os.getenv("DISCORD_GUILD_ID", "")

    # Webhook & API Server
    WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8000"))


settings = Settings()
