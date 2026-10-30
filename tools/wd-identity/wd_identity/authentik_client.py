"""
Authentik API client for invitation links, recovery links, and user management.
"""

import logging
import subprocess
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse
import requests

from wd_identity.config import settings

logger = logging.getLogger(__name__)


class AuthentikClient:
    def __init__(self):
        self.api_url = settings.AUTHENTIK_API_URL
        self.base_url = settings.AUTHENTIK_BASE_URL
        self.token = settings.AUTHENTIK_TOKEN
        self.invitation_flow_uuid = settings.AUTHENTIK_INVITATION_FLOW_UUID
        self.invitation_flow_slug = settings.AUTHENTIK_INVITATION_FLOW_SLUG
        self.recovery_flow_slug = settings.AUTHENTIK_RECOVERY_FLOW_SLUG

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _replace_with_public_url(self, raw_url: str) -> str:
        """Replaces localhost/internal host with the public auth domain."""
        parsed_target = urlparse(self.base_url)
        parsed_raw = urlparse(raw_url)
        replaced = parsed_raw._replace(
            scheme=parsed_target.scheme,
            netloc=parsed_target.netloc,
        )
        return urlunparse(replaced)

    def create_invitation(
        self,
        name: str,
        single_use: bool = True,
        fixed_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Creates a single-use invitation in Authentik and returns the enrollment URL.
        """
        url = f"{self.api_url}/api/v3/stages/invitation/invitations/"
        payload = {
            "name": name,
            "flow": self.invitation_flow_uuid,
            "single_use": single_use,
            "fixed_data": fixed_data or {},
        }
        res = requests.post(url, headers=self._headers, json=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        token_uuid = data["pk"]

        invite_link = f"{self.base_url}/if/flow/{self.invitation_flow_slug}/?itoken={token_uuid}"
        return invite_link

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Finds user in Authentik by username."""
        url = f"{self.api_url}/api/v3/core/users/"
        res = requests.get(
            url,
            headers=self._headers,
            params={"username": username},
            timeout=10,
        )
        res.raise_for_status()
        results = res.json().get("results", [])
        if not results:
            # Check case-insensitive
            res_all = requests.get(url, headers=self._headers, timeout=10)
            if res_all.ok:
                for u in res_all.json().get("results", []):
                    if u.get("username", "").lower() == username.lower():
                        return u
            return None
        return results[0]

    def create_recovery_link(self, username: str) -> Optional[str]:
        """
        Generates a single-use recovery link for the specified user.
        """
        user = self.get_user_by_username(username)
        if not user:
            # Trigger sync and try once more
            self.trigger_ldap_sync()
            user = self.get_user_by_username(username)

        if not user:
            logger.warning(f"User {username} not found in Authentik")
            return None

        user_pk = user["pk"]
        url = f"{self.api_url}/api/v3/core/users/{user_pk}/recovery/"
        res = requests.post(url, headers=self._headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        raw_link = data.get("link", "")
        if not raw_link:
            return None

        return self._replace_with_public_url(raw_link)

    def activate_user(self, username: str) -> bool:
        """Activates user in Authentik."""
        user = self.get_user_by_username(username)
        if not user:
            return False
        user_pk = user["pk"]
        url = f"{self.api_url}/api/v3/core/users/{user_pk}/"
        res = requests.patch(
            url,
            headers=self._headers,
            json={"is_active": True},
            timeout=10,
        )
        return res.ok

    def deactivate_user(self, username: str) -> bool:
        """Deactivates user in Authentik."""
        if username.lower() in ("tigimax", "akadmin"):
            logger.warning(f"Refusing to deactivate protected internal account '{username}' in Authentik.")
            return False
        user = self.get_user_by_username(username)
        if not user:
            return False
        user_pk = user["pk"]
        url = f"{self.api_url}/api/v3/core/users/{user_pk}/"
        res = requests.patch(
            url,
            headers=self._headers,
            json={"is_active": False},
            timeout=10,
        )
        return res.ok

    def set_user_banned(self, username: str, is_banned: bool = True) -> bool:
        """
        Sets or unsets the is_banned flag in Authentik user attributes.
        Keeps Authentik account active while blocking BlueMap via wd-bluemap-access-policy.
        """
        if is_banned and username.lower() in ("tigimax", "akadmin"):
            logger.warning(f"Refusing to ban protected internal account '{username}' in Authentik.")
            return False
        user = self.get_user_by_username(username)
        if not user:
            return False
        user_pk = user["pk"]
        current_attrs = user.get("attributes") or {}
        current_attrs["is_banned"] = bool(is_banned)
        url = f"{self.api_url}/api/v3/core/users/{user_pk}/"
        res = requests.patch(
            url,
            headers=self._headers,
            json={"attributes": current_attrs},
            timeout=10,
        )
        return res.ok

    def delete_user(self, username: str) -> bool:
        """Deletes user in Authentik so they can be re-enrolled."""
        if username.lower() in ("tigimax", "akadmin"):
            logger.warning(f"Refusing to delete protected internal account '{username}' in Authentik.")
            return False
        user = self.get_user_by_username(username)
        if not user:
            return True
        user_pk = user["pk"]
        url = f"{self.api_url}/api/v3/core/users/{user_pk}/"
        res = requests.delete(url, headers=self._headers, timeout=10)
        return res.status_code in (200, 204)

    def delete_invitations(self, username_or_prefix: str) -> int:
        """Deletes any invitations whose name contains the username or prefix."""
        url = f"{self.api_url}/api/v3/stages/invitation/invitations/"
        count = 0
        try:
            res = requests.get(
                url,
                headers=self._headers,
                params={"search": username_or_prefix},
                timeout=10,
            )
            if res.ok:
                for inv in res.json().get("results", []):
                    inv_pk = inv.get("pk")
                    inv_name = inv.get("name", "")
                    if username_or_prefix.lower() in inv_name.lower():
                        del_res = requests.delete(
                            f"{url}{inv_pk}/",
                            headers=self._headers,
                            timeout=10,
                        )
                        if del_res.status_code in (200, 204):
                            count += 1
        except Exception as e:
            logger.warning(f"Failed to delete invitations for {username_or_prefix}: {e}")
        return count

    def trigger_ldap_sync(self) -> bool:
        """Triggers OpenLDAP source sync in Authentik."""
        try:
            # Dispatch task via ak shell on the host
            cmd = [
                "docker",
                "exec",
                "-i",
                "authentik-server-1",
                "ak",
                "shell",
                "-c",
                "from authentik.sources.ldap.models import LDAPSource; from authentik.sources.ldap.tasks import ldap_sync; s = LDAPSource.objects.first(); ldap_sync.send(s.pk) if s else None",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return res.returncode == 0
        except Exception as e:
            logger.warning(f"Could not trigger direct ak shell LDAP sync: {e}")
            return False

    def add_user_to_group(self, username: str, group_name: str) -> bool:
        """Adds an Authentik user to a group by group name."""
        user = self.get_user_by_username(username)
        if not user:
            return False
        user_pk = user.get("pk")
        try:
            req_url = f"{self.api_url}/api/v3/core/groups/?name={group_name}"
            res = requests.get(req_url, headers=self._headers, timeout=10)
            if not res.ok:
                return False
            results = res.json().get("results", [])
            if not results:
                return False
            group_pk = results[0]["pk"]

            add_url = f"{self.api_url}/api/v3/core/groups/{group_pk}/add_user/"
            add_res = requests.post(add_url, headers=self._headers, json={"pk": user_pk}, timeout=10)
            return add_res.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to add {username} to Authentik group {group_name}: {e}")
            return False

    def get_group_by_name(self, group_name: str) -> Optional[Dict[str, Any]]:
        """Retrieves an Authentik group by its name."""
        try:
            url = f"{self.api_url}/api/v3/core/groups/?name={group_name}"
            res = requests.get(url, headers=self._headers, timeout=10)
            if not res.ok:
                return None
            results = res.json().get("results", [])
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Failed to query Authentik group '{group_name}': {e}")
            return None

    def set_project_launched(self, launched: bool) -> bool:
        """
        Updates the 'is_launched' flag on the 'wd_players' group in Authentik.
        When False (Pre-Launch), regular players are blocked from BlueMap via wd-bluemap-access-policy.
        When True (Live), regular players are allowed to access BlueMap.
        """
        group = self.get_group_by_name("wd_players")
        if not group:
            logger.warning("Authentik group 'wd_players' not found; cannot set project launched state.")
            return False

        group_pk = group["pk"]
        current_attrs = group.get("attributes") or {}
        current_attrs["is_launched"] = bool(launched)

        try:
            url = f"{self.api_url}/api/v3/core/groups/{group_pk}/"
            res = requests.patch(
                url,
                headers=self._headers,
                json={"attributes": current_attrs},
                timeout=10,
            )
            if res.ok:
                state_str = "LIVE (BlueMap unlocked for players)" if launched else "PRE-LAUNCH (BlueMap locked for players)"
                logger.info(f"Authentik BlueMap launch gate updated: {state_str}")
                return True
            logger.error(f"Failed to patch 'wd_players' attributes in Authentik: {res.status_code} {res.text}")
            return False
        except Exception as e:
            logger.error(f"Exception setting project launched flag in Authentik: {e}")
            return False

    def get_project_launched(self) -> Optional[bool]:
        """Reads the 'is_launched' flag from the 'wd_players' group in Authentik."""
        group = self.get_group_by_name("wd_players")
        if not group:
            return None
        attrs = group.get("attributes") or {}
        return bool(attrs.get("is_launched", False))

    def _execute_psql(
        self, sql_script: str, variables: Optional[Dict[str, str]] = None
    ) -> subprocess.CompletedProcess:
        """Executes a SQL script against the Authentik PostgreSQL container."""
        cmd = [
            "docker",
            "exec",
            "-i",
            "authentik-postgresql-1",
            "psql",
            "-U",
            "authentik",
            "-d",
            "authentik",
            "-v",
            "ON_ERROR_STOP=1",
            "-t",
            "-A",
        ]
        if variables:
            for k, v in variables.items():
                cmd.extend(["-v", f"{k}={v}"])
        return subprocess.run(
            cmd,
            input=sql_script,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def ensure_player_mfa_device(self, username: str) -> bool:
        """
        Ensures a confirmed StaticDevice ('Discord Bot Code') exists for the user.
        Allows regular players to authenticate via 6-digit one-time codes generated by Discord bot.
        """
        user = self.get_user_by_username(username)
        if not user:
            self.trigger_ldap_sync()
            user = self.get_user_by_username(username)
        if not user:
            logger.warning(f"User '{username}' not found in Authentik")
            return False

        user_id = str(user["pk"])
        sql = """
        INSERT INTO authentik_stages_authenticator_static_staticdevice
            (name, confirmed, user_id, throttling_failure_count, created, last_updated)
        SELECT 'Discord Bot Code', true, CAST(:'uid' AS integer), 0, NOW(), NOW()
        WHERE NOT EXISTS (
            SELECT 1 FROM authentik_stages_authenticator_static_staticdevice sd
            WHERE sd.user_id = CAST(:'uid' AS integer)
        );
        """
        try:
            res = self._execute_psql(sql, {"uid": user_id})
            if res.returncode == 0:
                return True
            logger.error(f"Failed to ensure player MFA device for {username}: {res.stderr}")
            return False
        except Exception as e:
            logger.error(f"Exception ensuring player MFA device for {username}: {e}")
            return False

    def has_totp_device(self, username: str) -> bool:
        """Checks if a user has a confirmed TOTP device in Authentik."""
        user = self.get_user_by_username(username)
        if not user:
            return False
        user_id = str(user["pk"])
        sql = """
        SELECT count(*)
        FROM authentik_stages_authenticator_totp_totpdevice d
        WHERE d.user_id = CAST(:'uid' AS integer) AND d.confirmed = true;
        """
        try:
            res = self._execute_psql(sql, {"uid": user_id})
            if res.returncode == 0 and res.stdout.strip().isdigit():
                return int(res.stdout.strip()) > 0
            return False
        except Exception as e:
            logger.error(f"Exception checking TOTP device for {username}: {e}")
            return False

    def generate_static_token_for_user(self, username: str) -> Optional[str]:
        """
        Generates a fresh 6-digit one-time login code for the given user,
        storing it as an active StaticToken in Authentik.
        Invalidates any previous unused static codes for this user.
        """
        import secrets
        code = f"{secrets.randbelow(900000) + 100000}"

        user = self.get_user_by_username(username)
        if not user:
            self.trigger_ldap_sync()
            user = self.get_user_by_username(username)
        if not user:
            logger.warning(f"User '{username}' not found in Authentik")
            return None

        user_id = str(user["pk"])
        self.ensure_player_mfa_device(username)

        sql = """
        DELETE FROM authentik_stages_authenticator_static_statictoken
        WHERE device_id IN (
            SELECT id FROM authentik_stages_authenticator_static_staticdevice
            WHERE user_id = CAST(:'uid' AS integer)
        );

        INSERT INTO authentik_stages_authenticator_static_statictoken (token, device_id)
        SELECT :'tok', id
        FROM authentik_stages_authenticator_static_staticdevice
        WHERE user_id = CAST(:'uid' AS integer);
        """
        try:
            res = self._execute_psql(sql, {"uid": user_id, "tok": code})
            if res.returncode == 0:
                logger.info(f"Generated static MFA token for user '{username}' (pk={user_id})")
                return code
            logger.error(f"Failed to generate static MFA token for {username}: {res.stderr}")
            return None
        except Exception as e:
            logger.error(f"Exception generating static MFA token for {username}: {e}")
            return None


