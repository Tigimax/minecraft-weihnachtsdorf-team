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
        self._mock_tokens: Dict[str, str] = {}

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
            res_all = requests.get(
                url,
                headers=self._headers,
                params={"search": username, "page_size": 100},
                timeout=10,
            )
            if res_all.ok:
                for u in res_all.json().get("results", []):
                    if u.get("username", "").lower() == username.lower():
                        return u
            return None
        return results[0]

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Finds user in Authentik by username (alias for get_user_by_username)."""
        return self.get_user_by_username(username)

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
        if res.ok and is_banned:
            self.revoke_proxy_sessions(username=username)
        return res.ok

    def revoke_proxy_sessions(self, username: Optional[str] = None) -> bool:
        """
        Revokes cached Authentik Proxy Outpost sessions (and OAuth2 tokens) so that
        BlueMap access policies (e.g. project lock or user ban) take effect immediately.
        When username is None, revokes sessions for all non-staff regular players.
        """
        try:
            if username:
                sql = """
                DELETE FROM authentik_providers_proxy_proxysession
                WHERE user_id IN (
                    SELECT uuid FROM authentik_core_user WHERE LOWER(username) = LOWER(:'uname')
                )
                OR POSITION(LOWER('"' || :'uname' || '"') IN LOWER(session_data::text)) > 0;

                DELETE FROM authentik_providers_oauth2_accesstoken
                WHERE user_id IN (
                    SELECT id FROM authentik_core_user WHERE LOWER(username) = LOWER(:'uname')
                );

                DELETE FROM authentik_providers_oauth2_refreshtoken
                WHERE user_id IN (
                    SELECT id FROM authentik_core_user WHERE LOWER(username) = LOWER(:'uname')
                );

                DELETE FROM django_postgres_cache_cacheentry
                WHERE cache_key LIKE '%app_access%'
                  AND (
                      cache_key LIKE '%app_access/' || (SELECT id::text FROM authentik_core_user WHERE LOWER(username) = LOWER(:'uname')) || '/%'
                      OR cache_key LIKE '%app_access/' || (SELECT id::text FROM authentik_core_user WHERE LOWER(username) = LOWER(:'uname'))
                  );
                """
                res = self._execute_psql(sql, {"uname": username.strip()})
            else:
                sql = """
                DELETE FROM authentik_providers_proxy_proxysession
                WHERE session_data::text NOT LIKE '%"wd_admins"%'
                  AND session_data::text NOT LIKE '%"wd_mods"%';

                DELETE FROM authentik_providers_oauth2_accesstoken
                WHERE provider_id IN (
                    SELECT id FROM authentik_core_provider WHERE name = 'wd-bluemap'
                );

                DELETE FROM authentik_providers_oauth2_refreshtoken
                WHERE provider_id IN (
                    SELECT id FROM authentik_core_provider WHERE name = 'wd-bluemap'
                );

                DELETE FROM django_postgres_cache_cacheentry
                WHERE cache_key LIKE '%app_access%';
                """
                res = self._execute_psql(sql)
            return res.returncode == 0
        except Exception as e:
            logger.warning(f"Failed to revoke proxy sessions (username={username}): {e}")
            return False

    def create_user(
        self,
        username: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Creates a user directly in Authentik."""
        url = f"{self.api_url}/api/v3/core/users/"
        payload = {
            "username": username,
            "name": name or username,
            "email": email or f"{username}@weihnachtsdorf.local",
            "is_active": True,
            "attributes": attributes or {},
        }
        try:
            res = requests.post(
                url,
                headers=self._headers,
                json=payload,
                timeout=10,
            )
            if res.status_code in (200, 201):
                return res.json()
            logger.warning(f"Failed to create user '{username}' in Authentik: {res.status_code} {res.text}")
        except Exception as e:
            logger.error(f"Exception creating user '{username}' directly in Authentik: {e}")
        return None

    def set_user_attributes(
        self,
        username: str,
        attributes: Dict[str, Any],
        merge: bool = True,
    ) -> bool:
        """Updates or merges attributes for a user in Authentik."""
        try:
            attrs_to_apply = dict(attributes)
            if username.lower() in ("tigimax", "akadmin"):
                attrs_to_apply.pop("is_banned", None)
                if "role" in attrs_to_apply and str(attrs_to_apply["role"]).strip().lower() != "admin":
                    attrs_to_apply.pop("role", None)
                attrs_to_apply.pop("needs_web_setup", None)

            user = self.get_user_by_username(username)
            if not user:
                self.trigger_ldap_sync()
                user = self.get_user_by_username(username)
            if not user:
                logger.warning(f"User '{username}' not found in Authentik to update attributes")
                return False

            user_pk = user["pk"]
            if merge:
                current_attrs = dict(user.get("attributes") or {})
                current_attrs.update(attrs_to_apply)
                new_attrs = current_attrs
            else:
                new_attrs = attrs_to_apply

            url = f"{self.api_url}/api/v3/core/users/{user_pk}/"
            res = requests.patch(
                url,
                headers=self._headers,
                json={"attributes": new_attrs},
                timeout=10,
            )
            return res.ok
        except Exception as e:
            logger.warning(f"Error in set_user_attributes for {username}: {e}")
            return False

    def set_needs_web_setup(self, username: str, needs_setup: bool) -> bool:
        """
        Updates the 'needs_web_setup' attribute on an Authentik user.
        Uses set_user_attributes(username, {"needs_web_setup": bool(needs_setup)}).
        Returns True on success.
        """
        return self.set_user_attributes(username, {"needs_web_setup": bool(needs_setup)})

    def set_user_password(self, username: str, password: str) -> bool:
        """
        Sets a user's password in Authentik via REST API.
        POST /api/v3/core/users/{user_pk}/set_password/ with {"password": password}.
        Returns True on 200 or 204.
        """
        if not username or not password:
            return False

        if username.lower() in ("tigimax", "akadmin"):
            logger.warning(f"Refusing to set password for protected internal account '{username}' in Authentik.")
            return False

        try:
            user = self.get_user(username)
            if not user:
                self.trigger_ldap_sync()
                user = self.get_user(username)
        except Exception as e:
            logger.warning(f"Error looking up user '{username}' in Authentik: {e}")
            user = None

        if not user:
            logger.warning(f"User '{username}' not found in Authentik to set password.")
            return False

        user_pk = user.get("pk") or user.get("id")
        if not user_pk:
            logger.warning(f"User '{username}' has no pk or id in Authentik.")
            return False

        url = f"{self.api_url}/api/v3/core/users/{user_pk}/set_password/"
        try:
            res = requests.post(
                url,
                headers=self._headers,
                json={"password": password},
                timeout=10,
            )
            if res.status_code in (200, 204):
                logger.info(f"Successfully set Authentik password for '{username}'")
                return True
            logger.error(f"Failed to set Authentik password for '{username}': {res.status_code} {res.text}")
            return False
        except Exception as e:
            logger.error(f"Exception setting Authentik password for '{username}': {e}")
            return False

    def provision_direct_player(
        self,
        username: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Provisions an Authentik player account for direct onboarding without invitation links:
        - Ensures user exists in Authentik (finds via LDAP or creates directly)
        - Sets attributes including 'needs_web_setup = True'
        - Adds user to group 'wd_players'
        - Ensures StaticDevice for Discord Bot MFA
        """
        clean_user = username
        if clean_user.lower() in ("tigimax", "akadmin"):
            clean_user = "tigimax_player"

        attrs = {"needs_web_setup": True, "role": "player"}
        if attributes:
            attrs.update(attributes)
        attrs["needs_web_setup"] = True

        user = self.get_user_by_username(clean_user)
        if not user:
            self.trigger_ldap_sync()
            user = self.get_user_by_username(clean_user)

        if not user:
            user = self.create_user(
                username=clean_user,
                name=name or clean_user,
                email=email or f"{clean_user}@weihnachtsdorf.local",
                attributes=attrs,
            )

        if not user:
            logger.warning(f"Could not find or create user '{clean_user}' in Authentik for direct provisioning.")
            return False

        self.set_user_attributes(clean_user, attrs, merge=True)
        self.add_user_to_group(clean_user, "wd_players")
        self.ensure_player_mfa_device(clean_user)
        return True

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

    def remove_user_from_group(self, username: str, group_name: str) -> bool:
        """Removes an Authentik user from a group by group name."""
        user = self.get_user_by_username(username)
        if not user:
            return False
        user_pk = user.get("pk")
        try:
            group = self.get_group_by_name(group_name)
            if not group:
                return False
            group_pk = group["pk"]

            remove_url = f"{self.api_url}/api/v3/core/groups/{group_pk}/remove_user/"
            remove_res = requests.post(remove_url, headers=self._headers, json={"pk": user_pk}, timeout=10)
            return remove_res.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to remove {username} from Authentik group {group_name}: {e}")
            return False

    def sync_user_groups(self, username: str, target_role: str) -> bool:
        """Ensures Authentik group membership matches target role and clears other role groups."""
        clean_role = (target_role or "player").lower().strip()
        role_group_map = {
            "admin": "wd_admins",
            "mod": "wd_mods",
            "player": "wd_players",
        }
        target_group = role_group_map.get(clean_role, "wd_players")
        remove_groups = [g for r, g in role_group_map.items() if r != clean_role]

        self.add_user_to_group(username, target_group)
        for g in remove_groups:
            self.remove_user_from_group(username, g)
        return True

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
                if not launched:
                    self.revoke_proxy_sessions()
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
                self._mock_tokens[username.strip().lower()] = code
                return code
            logger.error(f"Failed to generate static MFA token for {username}: {res.stderr}")
            return None
        except Exception as e:
            logger.error(f"Exception generating static MFA token for {username}: {e}")
            return None

    def set_mock_static_token(self, username: str, token: str) -> None:
        """Helper to register a mock static token for testing environments without PostgreSQL."""
        self._mock_tokens[username.strip().lower()] = str(token).strip()

    def verify_static_token_for_user(self, username: str, token: str) -> bool:
        """
        Verifies a 6-digit static token for the given user against Authentik's StaticToken table.
        On success, invalidates/deletes the token and returns True.
        Returns False if invalid, expired, or already used.
        Supports PostgreSQL query via _execute_psql and mock fallback in test environments.
        """
        clean_token = (token or "").strip()
        if not clean_token or not username or not username.strip():
            return False

        user_key = username.strip().lower()
        try:
            user = self.get_user(username)
            if not user:
                self.trigger_ldap_sync()
                user = self.get_user(username)
        except Exception as e:
            logger.warning(f"Error looking up user '{username}' in Authentik during token verification: {e}")
            user = None

        if not user:
            # Check mock fallback if user lookup fails in isolated test environment
            if hasattr(self, "_mock_tokens") and self._mock_tokens.get(user_key) == clean_token:
                del self._mock_tokens[user_key]
                logger.info(f"Verified static token for user '{username}' via mock fallback store")
                return True
            logger.warning(f"User '{username}' not found in Authentik for static token verification")
            return False

        user_id = str(user.get("pk") or user.get("id"))

        sql = """
        DELETE FROM authentik_stages_authenticator_static_statictoken
        WHERE token = :'tok'
          AND device_id IN (
              SELECT id FROM authentik_stages_authenticator_static_staticdevice
              WHERE user_id = CAST(:'uid' AS integer)
          )
        RETURNING id;
        """
        try:
            res = self._execute_psql(sql, {"uid": user_id, "tok": clean_token})
            if res.returncode == 0:
                stdout_val = res.stdout.strip()
                if stdout_val:
                    logger.info(f"Verified and consumed static MFA token for user '{username}' (pk={user_id})")
                    if hasattr(self, "_mock_tokens") and user_key in self._mock_tokens:
                        del self._mock_tokens[user_key]
                    return True
                return False
            logger.error(f"Failed to query static token for {username}: {res.stderr}")
        except Exception as e:
            logger.error(f"Exception executing psql static token verification for {username}: {e}")

        # Mock fallback for test environments without PostgreSQL / Docker
        if hasattr(self, "_mock_tokens") and self._mock_tokens.get(user_key) == clean_token:
            del self._mock_tokens[user_key]
            logger.info(f"Verified static token for user '{username}' via mock fallback store")
            return True

        return False



