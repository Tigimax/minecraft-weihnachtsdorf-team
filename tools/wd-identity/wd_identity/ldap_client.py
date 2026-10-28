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
from ldap3.utils.conv import escape_filter_chars

from wd_identity.config import settings
from wd_identity.utils import sanitize_email, sanitize_username, validate_minecraft_ign

logger = logging.getLogger(__name__)

HOMELAB_SUPERADMINS = ["tigimax"]
HOMELAB_SUPERADMIN_GROUPS = [
    "cn=proxmox_admins,ou=Groups,dc=ldap-01,dc=net",
    "cn=proxmox_users,ou=Groups,dc=ldap-01,dc=net",
    "cn=guac_admins,ou=Groups,dc=ldap-01,dc=net",
    "cn=guac_users,ou=Groups,dc=ldap-01,dc=net",
    "cn=homelab_admins,ou=Groups,dc=ldap-01,dc=net",
    "cn=tigimax_group,ou=Groups,dc=ldap-01,dc=net",
]


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
        self.player_group_dn = settings.LDAP_PLAYER_GROUP

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
        if not username or not username.strip():
            return None
        clean_user = sanitize_username(username)
        esc_user = escape_filter_chars(clean_user)
        conn = self._get_connection()
        try:
            user_filter = f"(&(objectClass=person)(uid={esc_user}))"
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
        if not discord_name_or_id or not discord_name_or_id.strip():
            return None
        target = discord_name_or_id.strip()
        esc_val = escape_filter_chars(target)
        clean_uid = sanitize_username(target)
        esc_uid = escape_filter_chars(clean_uid)

        conn = self._get_connection()
        try:
            # Exact match filter with escaped values
            user_filter = (
                f"(&(objectClass=person)(|(discordName={esc_val})"
                f"(discordId={esc_val})(uid={esc_val})(uid={esc_uid})))"
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
            target_lower = target.lower()
            for entry in conn.entries:
                data = self._format_entry(entry)
                if (
                    data["discordName"].lower() == target_lower
                    or data["discordId"] == target
                    or data["uid"].lower() == target_lower
                    or data["uid"].lower() == clean_uid
                ):
                    return data
            return None
        finally:
            conn.unbind()

    def search_by_minecraft(self, mc_name: str) -> Optional[Dict[str, Any]]:
        """Search user by Minecraft IGN."""
        if not mc_name or not mc_name.strip():
            return None
        target_ign = mc_name.strip()
        if not validate_minecraft_ign(target_ign):
            logger.warning(f"Invalid Minecraft IGN in search_by_minecraft: '{target_ign}'")
            return None

        esc_ign = escape_filter_chars(target_ign)
        conn = self._get_connection()
        try:
            user_filter = f"(&(objectClass=person)(minecraftName={esc_ign}))"
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
            target_lower = target_ign.lower()
            for entry in conn.entries:
                data = self._format_entry(entry)
                if data["minecraftName"].lower() == target_lower:
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

    def get_players(self) -> List[Dict[str, Any]]:
        """Fetch all registered regular players in cn=wd_players (excluding staff)."""
        admin_dns = set(self.get_group_members(self.admin_group_dn))
        mod_dns = set(self.get_group_members(self.mod_group_dn))
        staff_dns = admin_dns | mod_dns

        player_dns = set(self.get_group_members(self.player_group_dn))
        all_users = self.list_all_users()
        players = []

        for u in all_users:
            if u["dn"] in player_dns and u["dn"] not in staff_dns:
                u_copy = dict(u)
                u_copy["role"] = "player"
                players.append(u_copy)

        return players

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
        clean_user = sanitize_username(username)
        user_dn = f"uid={clean_user},{self.user_ou}"
        existing = self.get_user(clean_user)
        user_email = sanitize_email(clean_user, raw_email=email)

        clean_ign = minecraft_name.strip() if minecraft_name else None
        if clean_ign and not validate_minecraft_ign(clean_ign):
            logger.warning(f"Invalid Minecraft IGN '{clean_ign}' passed to create_or_update_user; omitting.")
            clean_ign = None

        conn = self._get_connection()
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
                    "discordName": discord_name.strip(),
                }
                if discord_id and discord_id.strip():
                    attributes["discordId"] = discord_id.strip()
                if clean_ign:
                    attributes["minecraftName"] = clean_ign
                if minecraft_uuid and minecraft_uuid.strip():
                    attributes["minecraftUuid"] = minecraft_uuid.strip()

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

                if discord_name and discord_name.strip():
                    modifications["discordName"] = [(MODIFY_REPLACE, [discord_name.strip()])]
                if discord_id and discord_id.strip():
                    modifications["discordId"] = [(MODIFY_REPLACE, [discord_id.strip()])]
                if clean_ign:
                    modifications["minecraftName"] = [(MODIFY_REPLACE, [clean_ign])]
                if minecraft_uuid and minecraft_uuid.strip():
                    modifications["minecraftUuid"] = [(MODIFY_REPLACE, [minecraft_uuid.strip()])]

                if modifications:
                    conn.modify(user_dn, modifications)

            # Assign group
            clean_role = role.lower().strip() if role else "admin"
            if clean_role == "admin":
                target_group = self.admin_group_dn
                remove_groups = [self.mod_group_dn, self.player_group_dn]
            elif clean_role == "mod":
                target_group = self.mod_group_dn
                remove_groups = [self.admin_group_dn, self.player_group_dn]
            else:
                clean_role = "player"
                target_group = self.player_group_dn
                remove_groups = [self.admin_group_dn, self.mod_group_dn]

            self.add_member_to_group(target_group, user_dn)
            for rg in remove_groups:
                self.remove_member_from_group(rg, user_dn)

            # Ensure homelab superadmin groups are retained/granted
            if clean_user in HOMELAB_SUPERADMINS:
                for g_dn in HOMELAB_SUPERADMIN_GROUPS:
                    self.add_member_to_group(g_dn, user_dn)

            # Fetch fresh state
            updated = self.get_user(clean_user)
            if updated:
                updated["role"] = clean_role
            return updated or {"uid": clean_user, "dn": user_dn, "role": clean_role}
        finally:
            conn.unbind()

    def set_minecraft_info(
        self, username: str, minecraft_name: str, minecraft_uuid: str
    ) -> bool:
        """Updates minecraftName and minecraftUuid on a user."""
        clean_user = sanitize_username(username)
        if not validate_minecraft_ign(minecraft_name):
            logger.error(f"Cannot set invalid Minecraft IGN: {minecraft_name}")
            return False

        clean_ign = minecraft_name.strip()
        clean_uuid = minecraft_uuid.strip()

        conn = self._get_connection()
        user_dn = f"uid={clean_user},{self.user_ou}"
        existing = self.get_user(clean_user)
        if not existing:
            logger.error(f"User {clean_user} not found in LDAP")
            return False

        try:
            modifications = {}
            if "wdAccount" not in existing.get("objectClass", []):
                modifications["objectClass"] = [(MODIFY_ADD, ["wdAccount"])]

            modifications["minecraftName"] = [(MODIFY_REPLACE, [clean_ign])]
            modifications["minecraftUuid"] = [(MODIFY_REPLACE, [clean_uuid])]

            success = conn.modify(user_dn, modifications)
            return success
        finally:
            conn.unbind()

    def revoke_roles(self, username: str) -> bool:
        """Removes user from admin, mod, and player LDAP groups."""
        clean_user = sanitize_username(username)
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
        s3 = self.remove_member_from_group(self.player_group_dn, user_dn)
        return s1 and s2 and s3

    def set_password(self, username: str, new_password: str) -> bool:
        """Sets the userPassword attribute for an LDAP user."""
        clean_user = sanitize_username(username)
        user_dn = f"uid={clean_user},{self.user_ou}"
        conn = self._get_connection()
        try:
            success = conn.modify(
                user_dn,
                {"userPassword": [(MODIFY_REPLACE, [new_password])]},
            )
            if success:
                logger.info(f"Updated userPassword for {clean_user} in OpenLDAP.")
            else:
                logger.error(f"Failed to update userPassword for {clean_user}: {conn.result}")
            return success
        except Exception as e:
            logger.error(f"Exception setting password for {clean_user}: {e}")
            return False
        finally:
            conn.unbind()

    def delete_user(self, username: str) -> bool:
        """Deletes a user entry from OpenLDAP."""
        clean_user = sanitize_username(username)
        user_dn = f"uid={clean_user},{self.user_ou}"
        conn = self._get_connection()
        try:
            success = conn.delete(user_dn)
            if success:
                logger.info(f"Deleted user {user_dn} from OpenLDAP.")
            return success
        except Exception as e:
            logger.error(f"Failed to delete {user_dn} from OpenLDAP: {e}")
            return False
        finally:
            conn.unbind()

    def clear_minecraft_info(self, username: str) -> bool:
        """Clears minecraftName and minecraftUuid attributes."""
        clean_user = sanitize_username(username)
        user_dn = f"uid={clean_user},{self.user_ou}"
        conn = self._get_connection()
        try:
            conn.modify(
                user_dn,
                {
                    "minecraftName": [(MODIFY_DELETE, [])],
                    "minecraftUuid": [(MODIFY_DELETE, [])],
                },
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to clear Minecraft info for {clean_user}: {e}")
            return False
        finally:
            conn.unbind()
