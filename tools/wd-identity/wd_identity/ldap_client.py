"""
LDAP client for Weihnachtsdorf 7 identity management.
Handles OpenLDAP user provisioning, group memberships, and wdAccount schema attributes.
"""

import logging
import secrets
import ssl
import string
from typing import Any, Dict, List, Optional, Tuple
from ldap3 import (
    ALL,
    Connection,
    MODIFY_ADD,
    MODIFY_DELETE,
    MODIFY_REPLACE,
    Server,
    Tls,
)
from ldap3.core.exceptions import (
    LDAPEntryAlreadyExistsResult,
    LDAPNoSuchAttributeResult,
    LDAPNoSuchObjectResult,
)

from wd_identity.config import settings

logger = logging.getLogger(__name__)


class LDAPClient:
    def __init__(self):
        self.server_uri = settings.LDAP_URI
        self.bind_dn = settings.LDAP_BIND_DN
        self.bind_password = settings.LDAP_BIND_PASSWORD
        self.base_dn = settings.LDAP_BASE_DN
        self.user_ou = settings.LDAP_USER_OU
        self.group_ou = settings.LDAP_GROUP_OU
        self.admin_group_dn = settings.LDAP_ADMIN_GROUP
        self.mod_group_dn = settings.LDAP_MOD_GROUP

    def _get_connection(self) -> Connection:
        tls = Tls(validate=ssl.CERT_NONE)
        server = Server(self.server_uri, use_ssl=True, tls=tls, get_info=ALL)
        conn = Connection(
            server,
            user=self.bind_dn,
            password=self.bind_password,
            auto_bind=True,
        )
        return conn

    @staticmethod
    def _format_entry(entry) -> Dict[str, Any]:
        d = entry.entry_attributes_as_dict
        uid_list = d.get("uid", [])
        uid = uid_list[0] if uid_list else ""
        discord_name = d.get("discordName", [])
        discord_id = d.get("discordId", [])
        mc_name = d.get("minecraftName", [])
        mc_uuid = d.get("minecraftUuid", [])
        mail = d.get("mail", [])
        cn = d.get("cn", [])
        object_classes = d.get("objectClass", [])

        return {
            "dn": entry.entry_dn,
            "uid": uid,
            "cn": cn[0] if cn else uid,
            "mail": mail[0] if mail else f"{uid}@tigimax.mooo.com",
            "discordName": discord_name[0] if discord_name else "",
            "discordId": discord_id[0] if discord_id else "",
            "minecraftName": mc_name[0] if mc_name else "",
            "minecraftUuid": mc_uuid[0] if mc_uuid else "",
            "objectClass": object_classes,
        }

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Retrieve user by UID."""
        conn = self._get_connection()
        try:
            user_filter = f"(&(objectClass=person)(uid={username}))"
            conn.search(
                self.user_ou,
                user_filter,
                attributes=[
                    "uid",
                    "cn",
                    "sn",
                    "mail",
                    "objectClass",
                    "discordName",
                    "discordId",
                    "minecraftName",
                    "minecraftUuid",
                ],
            )
            if not conn.entries:
                return None
            return self._format_entry(conn.entries[0])
        finally:
            conn.unbind()

    def search_by_discord(self, discord_name_or_id: str) -> Optional[Dict[str, Any]]:
        """Search user by discordName or discordId or UID."""
        conn = self._get_connection()
        try:
            # First try exact discordName or discordId
            user_filter = (
                f"(&(objectClass=person)(|(discordName={discord_name_or_id})"
                f"(discordId={discord_name_or_id})(uid={discord_name_or_id})))"
            )
            conn.search(
                self.user_ou,
                user_filter,
                attributes=[
                    "uid",
                    "cn",
                    "sn",
                    "mail",
                    "objectClass",
                    "discordName",
                    "discordId",
                    "minecraftName",
                    "minecraftUuid",
                ],
            )
            if conn.entries:
                return self._format_entry(conn.entries[0])

            # Try case-insensitive matching if not found
            conn.search(
                self.user_ou,
                "(objectClass=person)",
                attributes=[
                    "uid",
                    "cn",
                    "sn",
                    "mail",
                    "objectClass",
                    "discordName",
                    "discordId",
                    "minecraftName",
                    "minecraftUuid",
                ],
            )
            for entry in conn.entries:
                data = self._format_entry(entry)
                if (
                    data["discordName"].lower() == discord_name_or_id.lower()
                    or data["uid"].lower() == discord_name_or_id.lower()
                ):
                    return data
            return None
        finally:
            conn.unbind()

    def search_by_minecraft(self, mc_name: str) -> Optional[Dict[str, Any]]:
        """Search user by Minecraft IGN."""
        conn = self._get_connection()
        try:
            user_filter = f"(&(objectClass=person)(minecraftName={mc_name}))"
            conn.search(
                self.user_ou,
                user_filter,
                attributes=[
                    "uid",
                    "cn",
                    "sn",
                    "mail",
                    "objectClass",
                    "discordName",
                    "discordId",
                    "minecraftName",
                    "minecraftUuid",
                ],
            )
            if conn.entries:
                return self._format_entry(conn.entries[0])

            # Fallback scan
            conn.search(
                self.user_ou,
                "(objectClass=person)",
                attributes=[
                    "uid",
                    "cn",
                    "sn",
                    "mail",
                    "objectClass",
                    "discordName",
                    "discordId",
                    "minecraftName",
                    "minecraftUuid",
                ],
            )
            for entry in conn.entries:
                data = self._format_entry(entry)
                if data["minecraftName"].lower() == mc_name.lower():
                    return data
            return None
        finally:
            conn.unbind()

    def list_all_users(self) -> List[Dict[str, Any]]:
        """List all users under user_ou."""
        conn = self._get_connection()
        try:
            conn.search(
                self.user_ou,
                "(&(objectClass=person)(uid=*))",
                attributes=[
                    "uid",
                    "cn",
                    "sn",
                    "mail",
                    "objectClass",
                    "discordName",
                    "discordId",
                    "minecraftName",
                    "minecraftUuid",
                ],
            )
            return [self._format_entry(e) for e in conn.entries]
        finally:
            conn.unbind()

    def get_group_members(self, group_dn: str) -> List[str]:
        """Return list of member DNs in a group."""
        conn = self._get_connection()
        try:
            conn.search(
                group_dn,
                "(objectClass=*)",
                attributes=["member", "uniqueMember"],
            )
            if not conn.entries:
                return []
            entry = conn.entries[0]
            members = entry.entry_attributes_as_dict.get("member", [])
            return list(members)
        finally:
            conn.unbind()

    def add_member_to_group(self, group_dn: str, member_dn: str) -> bool:
        """Add a member DN to an LDAP group."""
        conn = self._get_connection()
        try:
            members = self.get_group_members(group_dn)
            if member_dn in members:
                return True
            success = conn.modify(
                group_dn,
                {"member": [(MODIFY_ADD, [member_dn])]},
            )
            return success
        except Exception as e:
            logger.error(f"Error adding {member_dn} to {group_dn}: {e}")
            return False
        finally:
            conn.unbind()

    def remove_member_from_group(self, group_dn: str, member_dn: str) -> bool:
        """Remove a member DN from an LDAP group."""
        conn = self._get_connection()
        try:
            members = self.get_group_members(group_dn)
            if member_dn not in members:
                return True
            success = conn.modify(
                group_dn,
                {"member": [(MODIFY_DELETE, [member_dn])]},
            )
            return success
        except (LDAPNoSuchAttributeResult, Exception) as e:
            logger.warning(f"Error removing {member_dn} from {group_dn}: {e}")
            return False
        finally:
            conn.unbind()

    def get_admins_and_mods(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Fetch all admins and mods with full attribute dictionaries."""
        admin_dns = set(self.get_group_members(self.admin_group_dn))
        mod_dns = set(self.get_group_members(self.mod_group_dn))
        all_users = self.list_all_users()

        admins = []
        mods = []

        for u in all_users:
            if u["dn"] in admin_dns:
                u_copy = dict(u)
                u_copy["role"] = "admin"
                admins.append(u_copy)
            elif u["dn"] in mod_dns:
                u_copy = dict(u)
                u_copy["role"] = "mod"
                mods.append(u_copy)

        return admins, mods

    def create_or_update_user(
        self,
        username: str,
        discord_name: str,
        discord_id: Optional[str] = None,
        email: Optional[str] = None,
        role: str = "admin",
        minecraft_name: Optional[str] = None,
        minecraft_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates or updates user with custom wdAccount attributes and group membership.
        Returns the user entry dict.
        """
        conn = self._get_connection()
        clean_user = username.lower().strip()
        user_dn = f"uid={clean_user},{self.user_ou}"
        existing = self.get_user(clean_user)

        user_email = email or f"{clean_user}@tigimax.mooo.com"

        try:
            if not existing:
                # Generate random temporary password
                chars = string.ascii_letters + string.digits
                tmp_pass = "".join(secrets.choice(chars) for _ in range(16))

                attributes = {
                    "objectClass": [
                        "top",
                        "person",
                        "organizationalPerson",
                        "inetOrgPerson",
                        "wdAccount",
                    ],
                    "cn": clean_user,
                    "sn": clean_user,
                    "uid": clean_user,
                    "mail": user_email,
                    "userPassword": tmp_pass,
                    "discordName": discord_name,
                }
                if discord_id:
                    attributes["discordId"] = discord_id
                if minecraft_name:
                    attributes["minecraftName"] = minecraft_name
                if minecraft_uuid:
                    attributes["minecraftUuid"] = minecraft_uuid

                success = conn.add(user_dn, attributes=attributes)
                if not success:
                    logger.error(f"Failed to add user {user_dn}: {conn.result}")
                    raise RuntimeError(f"LDAP add user failed: {conn.result}")
            else:
                # Update existing user
                modifications = {}
                obj_classes = existing.get("objectClass", [])
                if "wdAccount" not in obj_classes:
                    modifications["objectClass"] = [(MODIFY_ADD, ["wdAccount"])]

                modifications["discordName"] = [(MODIFY_REPLACE, [discord_name])]
                if discord_id:
                    modifications["discordId"] = [(MODIFY_REPLACE, [discord_id])]
                if minecraft_name:
                    modifications["minecraftName"] = [(MODIFY_REPLACE, [minecraft_name])]
                if minecraft_uuid:
                    modifications["minecraftUuid"] = [(MODIFY_REPLACE, [minecraft_uuid])]

                if modifications:
                    conn.modify(user_dn, modifications)

            # Assign group
            target_group = (
                self.admin_group_dn if role.lower() == "admin" else self.mod_group_dn
            )
            other_group = (
                self.mod_group_dn if role.lower() == "admin" else self.admin_group_dn
            )

            self.add_member_to_group(target_group, user_dn)
            self.remove_member_from_group(other_group, user_dn)

            # Fetch fresh state
            updated = self.get_user(clean_user)
            if updated:
                updated["role"] = role.lower()
            return updated or {"uid": clean_user, "dn": user_dn, "role": role.lower()}
        finally:
            conn.unbind()

    def set_minecraft_info(
        self, username: str, minecraft_name: str, minecraft_uuid: str
    ) -> bool:
        """Updates minecraftName and minecraftUuid on a user."""
        conn = self._get_connection()
        clean_user = username.lower().strip()
        user_dn = f"uid={clean_user},{self.user_ou}"
        existing = self.get_user(clean_user)
        if not existing:
            logger.error(f"User {clean_user} not found in LDAP")
            return False

        try:
            modifications = {}
            if "wdAccount" not in existing.get("objectClass", []):
                modifications["objectClass"] = [(MODIFY_ADD, ["wdAccount"])]

            modifications["minecraftName"] = [(MODIFY_REPLACE, [minecraft_name])]
            modifications["minecraftUuid"] = [(MODIFY_REPLACE, [minecraft_uuid])]

            success = conn.modify(user_dn, modifications)
            return success
        finally:
            conn.unbind()

    def revoke_roles(self, username: str) -> bool:
        """Removes user from both admin and mod LDAP groups."""
        clean_user = username.lower().strip()
        user = self.get_user(clean_user)
        if not user:
            # Check by discord
            user = self.search_by_discord(username)
        if not user:
            logger.warning(f"User {username} not found for role revocation")
            return False

        user_dn = user["dn"]
        s1 = self.remove_member_from_group(self.admin_group_dn, user_dn)
        s2 = self.remove_member_from_group(self.mod_group_dn, user_dn)
        return s1 and s2
