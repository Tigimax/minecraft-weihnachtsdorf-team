"""
Planka Kanban user provisioning and deactivation module.
Communicates with PostgreSQL inside the planka container on usrv01 via SSH.
"""

import logging
import subprocess
from typing import Optional
from wd_identity.config import settings

logger = logging.getLogger(__name__)


class PlankaSyncClient:
    def __init__(self):
        self.ssh_host = settings.PLANKA_SSH_HOST
        self.ssh_user = settings.PLANKA_SSH_USER
        self.container = settings.PLANKA_CONTAINER
        self.db_name = settings.PLANKA_DB_NAME
        self.db_user = settings.PLANKA_DB_USER

    def _execute_sql(self, sql: str) -> bool:
        """Executes a SQL statement in Planka Postgres container on usrv01."""
        escaped_sql = sql.replace('"', '\\"')
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            f"{self.ssh_user}@{self.ssh_host}",
            f'sudo docker exec {self.container} psql -U {self.db_user} -d {self.db_name} -c "{escaped_sql}"',
        ]
        try:
            res = subprocess.run(
                cmd,
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
        Creates or updates a Planka user account.
        """
        clean_user = username.strip().lower()
        clean_email = (email or f"{clean_user}@tigimax.mooo.com").strip().lower()
        clean_name = (name or clean_user.capitalize()).strip()
        clean_role = role.strip().lower()

        sql = f"""
        INSERT INTO user_account (
            email, role, name, username,
            subscribe_to_own_cards, subscribe_to_card_when_commenting,
            turn_off_recent_card_highlighting, enable_favorites_by_default,
            default_editor_mode, default_home_view, default_projects_order,
            is_deactivated, created_at, updated_at, auto_logout_mode
        ) VALUES (
            '{clean_email}', '{clean_role}', '{clean_name}', '{clean_user}',
            false, true, false, true,
            'wysiwyg', 'groupedProjects', 'byDefault',
            false, NOW(), NOW(), '30m'
        ) ON CONFLICT (email) DO UPDATE SET 
            role = EXCLUDED.role, 
            is_deactivated = false, 
            updated_at = NOW();
        """
        return self._execute_sql(sql)

    def deactivate_user(self, username: str, email: Optional[str] = None) -> bool:
        """
        Deactivates a user account in Planka.
        """
        clean_user = username.strip().lower()
        clean_email = (email or f"{clean_user}@tigimax.mooo.com").strip().lower()
        sql = f"""
        UPDATE user_account 
        SET is_deactivated = true, updated_at = NOW() 
        WHERE username = '{clean_user}' OR email = '{clean_email}';
        """
        return self._execute_sql(sql)
