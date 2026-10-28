"""
Planka Kanban user provisioning and deactivation module.
Communicates with PostgreSQL inside the planka container on usrv01 via SSH.
"""

import logging
import subprocess
from typing import Optional
from wd_identity.config import settings
from wd_identity.utils import escape_sql_literal, sanitize_email, sanitize_username

logger = logging.getLogger(__name__)


class PlankaSyncClient:
    def __init__(self):
        self.ssh_host = settings.PLANKA_SSH_HOST
        self.ssh_user = settings.PLANKA_SSH_USER
        self.container = settings.PLANKA_CONTAINER
        self.db_name = settings.PLANKA_DB_NAME
        self.db_user = settings.PLANKA_DB_USER

    def _execute_sql(self, sql: str) -> bool:
        """Executes a SQL statement in Planka Postgres container on usrv01 via stdin over SSH."""
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            f"{self.ssh_user}@{self.ssh_host}",
            f"sudo docker exec -i {self.container} psql -U {self.db_user} -d {self.db_name}",
        ]
        try:
            res = subprocess.run(
                cmd,
                input=sql,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                return True
            logger.error(
                f"Planka SQL failed with code {res.returncode}: {res.stderr.strip()}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to execute Planka SQL over SSH: {e}")
            return False

    def sync_user(
        self,
        username: str,
        email: Optional[str] = None,
        name: Optional[str] = None,
        role: str = "admin",
    ) -> bool:
        """
        Creates or updates a Planka user account safely.
        Handles both email and username matching to avoid exclusion constraint conflicts.
        """
        clean_user = sanitize_username(username)
        clean_email = sanitize_email(clean_user, raw_email=email)
        raw_name = (name or clean_user.capitalize()).strip()
        clean_role = role.strip().lower() if role else "admin"

        esc_user = escape_sql_literal(clean_user)
        esc_email = escape_sql_literal(clean_email)
        esc_name = escape_sql_literal(raw_name)
        esc_role = escape_sql_literal(clean_role)

        sql = f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM user_account WHERE username = '{esc_user}' OR email = '{esc_email}') THEN
                UPDATE user_account
                SET email = '{esc_email}',
                    role = '{esc_role}',
                    name = '{esc_name}',
                    username = '{esc_user}',
                    terms_signature = '2796eedee33c0bc4592eb8f0d301acffe60ca01d81e9df4023cb32d05f724323',
                    terms_accepted_at = COALESCE(terms_accepted_at, NOW()),
                    is_deactivated = false,
                    updated_at = NOW()
                WHERE username = '{esc_user}' OR email = '{esc_email}';
            ELSE
                INSERT INTO user_account (
                    email, role, name, username,
                    terms_signature, terms_accepted_at,
                    subscribe_to_own_cards, subscribe_to_card_when_commenting,
                    turn_off_recent_card_highlighting, enable_favorites_by_default,
                    default_editor_mode, default_home_view, default_projects_order,
                    is_deactivated, created_at, updated_at, auto_logout_mode
                ) VALUES (
                    '{esc_email}', '{esc_role}', '{esc_name}', '{esc_user}',
                    '2796eedee33c0bc4592eb8f0d301acffe60ca01d81e9df4023cb32d05f724323', NOW(),
                    false, true, false, true,
                    'wysiwyg', 'groupedProjects', 'byDefault',
                    false, NOW(), NOW(), '30m'
                );
            END IF;
        END $$;
        """
        return self._execute_sql(sql)

    def deactivate_user(self, username: str, email: Optional[str] = None) -> bool:
        """
        Deactivates a user account in Planka.
        """
        clean_user = sanitize_username(username)
        clean_email = sanitize_email(clean_user, raw_email=email)
        esc_user = escape_sql_literal(clean_user)
        esc_email = escape_sql_literal(clean_email)

        sql = f"""
        UPDATE user_account 
        SET is_deactivated = true, updated_at = NOW() 
        WHERE username = '{esc_user}' OR email = '{esc_email}';
        """
        return self._execute_sql(sql)
