"""
Membership and privilege lifecycle coordinator for Weihnachtsdorf 7.
Handles revoking privileges across LDAP, Authentik, Planka, Minecraft, and DB
when a user leaves the Discord server or when an admin revokes access while keeping the account intact.
"""

import logging
from typing import Any, Dict, Optional, Union

from wd_identity import applications
from wd_identity.audit_logger import COLOR_ERROR, COLOR_INFO, COLOR_WARNING, log_admin_event
from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.planka_sync import PlankaSyncClient
from wd_identity.playerlist import sync_playerlist_rest
from wd_identity.utils import sanitize_username

logger = logging.getLogger(__name__)


def revoke_user_privileges(
    target: Union[str, Dict[str, Any]],
    reason: str = "Discord-Server verlassen",
    initiator: str = "Discord Event",
) -> Dict[str, Any]:
    """
    Revokes all access rights across OpenLDAP, Authentik, Planka, and Minecraft,
    WHILE PRESERVING the account itself in LDAP and Authentik (in deactivated state).

    TARGET can be an LDAP user dict, Discord ID, Discord name, or username.
    """
    ldap = LDAPClient()
    authentik = AuthentikClient()
    planka = PlankaSyncClient()
    mc = MinecraftSyncClient()

    user = None
    target_str = ""

    if isinstance(target, dict) and "uid" in target:
        user = target
        target_str = user["uid"]
    else:
        target_str = str(target).strip()
        if target_str.isdigit():
            user = ldap.search_by_discord(target_str)
        if not user:
            user = (
                ldap.get_user(target_str)
                or ldap.search_by_discord(target_str)
                or ldap.search_by_minecraft(target_str)
            )

    if not user:
        logger.info(f"Target '{target_str}' not found in LDAP for privilege revocation.")
        if target_str.isdigit():
            try:
                status = applications.get_user_status(target_str)
                if status.get("has_pending") and status.get("pending_app"):
                    applications.reject_application(
                        status["pending_app"]["id"],
                        "system",
                        initiator,
                        f"Antrag abgebrochen: {reason}",
                    )
            except Exception as e:
                logger.warning(f"Failed to cancel pending application for {target_str}: {e}")

        return {
            "found": False,
            "target": target_str,
            "reason": reason,
        }

    username = user["uid"]
    user_dn = user["dn"]
    discord_id = user.get("discordId")
    discord_name = user.get("discordName")
    ign = user.get("minecraftName")
    mc_uuid = user.get("minecraftUuid")
    email = user.get("mail")

    logger.info(
        f"Revoking all privileges for user '{username}' (IGN: {ign}, Discord: {discord_name or discord_id}). Keeping account intact."
    )

    # 1. OpenLDAP: Remove all group memberships (wd_admins, wd_mods, wd_players)
    ldap_roles_ok = ldap.revoke_roles(username)
    ldap.remove_member_from_group(ldap.player_group_dn, user_dn)
    ldap.remove_member_from_group(ldap.admin_group_dn, user_dn)
    ldap.remove_member_from_group(ldap.mod_group_dn, user_dn)

    # 2. Authentik IdP: Deactivate user and block BlueMap policy, delete pending invitations
    auth_deact_ok = authentik.deactivate_user(username)
    auth_ban_ok = authentik.set_user_banned(username, True)
    inv_del_count = authentik.delete_invitations(username)
    if discord_name and discord_name != username:
        inv_del_count += authentik.delete_invitations(discord_name)
    if discord_id:
        inv_del_count += authentik.delete_invitations(discord_id)

    try:
        authentik.trigger_ldap_sync()
    except Exception as e:
        logger.warning(f"Failed to trigger LDAP sync in Authentik for {username}: {e}")

    # 3. Planka Kanban: Deactivate account
    planka_deact_ok = planka.deactivate_user(username, email=email)

    # 4. Minecraft Server: Whitelist remove, clear LuckPerms, kick if online
    mc_ok = False
    if ign:
        try:
            mc_ok = mc.revoke_player_privileges(ign, mc_uuid=mc_uuid)
            target_lp = mc_uuid if mc_uuid else ign
            mc.execute_commands([
                f"whitelist remove {ign}",
                f"lp user {target_lp} clear",
                f"lp user {target_lp} parent set default",
                f'kick {ign} "Rechte entzogen: {reason}"',
            ])
            mc.sync_all(force=True)
        except Exception as e:
            logger.error(f"Error revoking Minecraft privileges for {ign}: {e}")

    # 5. SQLite DB: Cancel any pending applications
    if discord_id:
        try:
            status = applications.get_user_status(discord_id)
            if status.get("has_pending") and status.get("pending_app"):
                applications.reject_application(
                    status["pending_app"]["id"],
                    "system",
                    initiator,
                    f"Rechte entzogen: {reason}",
                )
        except Exception as e:
            logger.warning(f"Error rejecting pending application for {discord_id}: {e}")

    # 6. Audit Logging to #admin-logs
    try:
        if "verlassen" in reason.lower():
            log_title = "🚪 Discord verlassen: Rechte entzogen (Account erhalten)"
            log_desc = (
                f"Dem Nutzer **{discord_name or username}** wurden alle Zugriffsrechte entzogen, "
                f"da der Discord-Server verlassen wurde.\n"
                f"Der Account bleibt inaktiv im System erhalten."
            )
        elif "selbstabmeldung" in reason.lower():
            log_title = "👋 Selbstabmeldung: Rechte entzogen (Account erhalten)"
            log_desc = (
                f"Dem Nutzer **{discord_name or username}** wurden alle Zugriffsrechte entzogen (Selbstabmeldung).\n"
                f"**Grund:** {reason}\n"
                f"Der Account bleibt inaktiv im System erhalten."
            )
        else:
            log_title = "🔒 Admin-Eingriff: Rechte entzogen (Account erhalten)"
            log_desc = (
                f"Dem Nutzer **{discord_name or username}** wurden alle Projekt-Zugriffsrechte durch **{initiator}** entzogen.\n"
                f"**Auslöser / Grund:** {reason}\n"
                f"Der Account bleibt inaktiv im System erhalten."
            )

        log_admin_event(
            title=log_title,
            description=log_desc,
            color=COLOR_WARNING,
            fields=[
                {"name": "Benutzername", "value": f"`{username}`", "inline": True},
                {"name": "Discord", "value": f"{discord_name or 'Unbekannt'} (`{discord_id or 'Keine ID'}`)", "inline": True},
                {"name": "Minecraft", "value": f"`{ign}` (Whitelist entfernt, Permissions geleert)" if ign else "Kein IGN hinterlegt", "inline": True},
                {"name": "OpenLDAP", "value": "Rollen & Gruppen entzogen (Account bleibt bestehen)", "inline": True},
                {"name": "Authentik / SSO", "value": "Deaktiviert (`is_active=False`, BlueMap gesperrt)", "inline": True},
                {"name": "Planka", "value": "Konto deaktiviert", "inline": True},
                {"name": "Ausgeführt von", "value": f"{initiator}", "inline": True},
                {"name": "Grund / Auslöser", "value": f"`{reason}`", "inline": True},
            ],
            author_name=initiator,
        )
    except Exception as e:
        logger.error(f"Failed to post audit log for revoked privileges of {username}: {e}")

    # 7. Synchronize Playerlist live embed
    try:
        sync_playerlist_rest(force=True)
    except Exception as e:
        logger.warning(f"Failed to update playerlist board after privilege revocation: {e}")

    return {
        "found": True,
        "username": username,
        "discord_id": discord_id,
        "discord_name": discord_name,
        "minecraft_name": ign,
        "minecraft_uuid": mc_uuid,
        "ldap_revoked": ldap_roles_ok,
        "authentik_deactivated": auth_deact_ok,
        "authentik_banned": auth_ban_ok,
        "planka_deactivated": planka_deact_ok,
        "minecraft_revoked": mc_ok,
    }


def restore_user_privileges(
    target: Union[str, Dict[str, Any]],
    role: str = "player",
    initiator: str = "Admin",
) -> Dict[str, Any]:
    """
    Restores access rights for an existing deactivated account.
    - Adds LDAP user to role group (wd_players or wd_admins if allowed)
    - Re-activates user in Authentik (is_active=True, is_banned=False)
    - Re-activates in Planka
    - Re-enables Minecraft whitelist & LuckPerms
    - Logs to #admin-logs & updates playerlist
    """
    clean_role = (role or "player").lower().strip()
    ldap = LDAPClient()
    authentik = AuthentikClient()
    planka = PlankaSyncClient()
    mc = MinecraftSyncClient()

    user = None
    target_str = ""

    if isinstance(target, dict) and "uid" in target:
        user = target
        target_str = user["uid"]
    else:
        target_str = str(target).strip()
        if target_str.isdigit():
            user = ldap.search_by_discord(target_str)
        if not user:
            user = (
                ldap.get_user(target_str)
                or ldap.search_by_discord(target_str)
                or ldap.search_by_minecraft(target_str)
            )

    if not user:
        raise ValueError(f"Benutzer '{target_str}' konnte in LDAP nicht gefunden werden.")

    username = user["uid"]
    user_dn = user["dn"]
    discord_id = user.get("discordId")
    discord_name = user.get("discordName")
    ign = user.get("minecraftName")
    mc_uuid = user.get("minecraftUuid")
    email = user.get("mail")

    # 1. OpenLDAP: assign to correct group
    target_group_dn = ldap.admin_group_dn if clean_role == "admin" else ldap.player_group_dn
    ldap.add_member_to_group(target_group_dn, user_dn)

    # 2. Authentik: activate & unban
    authentik.activate_user(username)
    authentik.set_user_banned(username, False)

    try:
        authentik.trigger_ldap_sync()
    except Exception as e:
        logger.warning(f"Failed to trigger LDAP sync in Authentik for {username}: {e}")

    # 3. Planka: re-sync user (reactivates)
    planka.sync_user(username=username, email=email, role=clean_role)

    # 4. Minecraft Server: whitelist & role
    mc_ok = False
    if ign:
        try:
            mc.whitelist_add(ign)
            mc.set_player_role(ign, clean_role, mc_uuid=mc_uuid)
            mc.sync_all(force=True)
            mc_ok = True
        except Exception as e:
            logger.error(f"Error restoring Minecraft privileges for {ign}: {e}")

    # 5. Audit log
    try:
        log_admin_event(
            title="🔄 Berechtigungen wiederhergestellt",
            description=f"Die Zugriffsrechte für **{discord_name or username}** wurden reaktiviert.",
            color=COLOR_INFO,
            fields=[
                {"name": "Benutzername", "value": f"`{username}`", "inline": True},
                {"name": "Rolle", "value": f"`{clean_role}`", "inline": True},
                {"name": "Minecraft IGN", "value": f"`{ign}`" if ign else "Kein IGN", "inline": True},
            ],
            author_name=initiator,
        )
    except Exception as e:
        logger.error(f"Failed to post audit log for restored privileges of {username}: {e}")

    # 6. Synchronize Playerlist live embed
    try:
        sync_playerlist_rest(force=True)
    except Exception as e:
        logger.warning(f"Failed to update playerlist board after privilege restoration: {e}")

    return {
        "success": True,
        "username": username,
        "role": clean_role,
        "minecraft_name": ign,
        "minecraft_uuid": mc_uuid,
    }
