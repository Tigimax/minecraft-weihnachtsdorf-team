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

    def deactivate_user(self, username: str) -> bool:
        """Deactivates user in Authentik."""
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

    def delete_user(self, username: str) -> bool:
        """Deletes user in Authentik so they can be re-enrolled."""
        user = self.get_user_by_username(username)
        if not user:
            return True
        user_pk = user["pk"]
        url = f"{self.api_url}/api/v3/core/users/{user_pk}/"
        res = requests.delete(url, headers=self._headers, timeout=10)
        return res.status_code in (200, 204)

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
                "from authentik.sources.ldap.tasks import ldap_sync_all; ldap_sync_all.delay()",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return res.returncode == 0
        except Exception as e:
            logger.warning(f"Could not trigger direct ak shell LDAP sync: {e}")
            return False
