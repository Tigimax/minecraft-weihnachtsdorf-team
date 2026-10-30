"""
Invitation coordinator for Weihnachtsdorf 7.
Centralizes invitation creation across CLI, Discord Bot Slash Commands, and UI Modals.
Enforces security checks (Discord Admin verification), OpenLDAP provisioning,
Authentik token generation, DM dispatch, audit logging, and participant list updates.
"""

import logging
from typing import Any, Dict, Optional

from wd_identity.audit_logger import COLOR_ERROR, COLOR_SUCCESS, COLOR_WARNING, log_admin_event
from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.mojang_client import get_player_profile
from wd_identity.playerlist import sync_playerlist_rest
from wd_identity.utils import (
    check_is_discord_admin,
    sanitize_email,
    sanitize_username,
    send_dm,
    validate_minecraft_ign,
)

logger = logging.getLogger(__name__)


def create_user_invitation(
    discord_name: str,
    role: str = "player",
    ign: Optional[str] = None,
    username: Optional[str] = None,
    inviter: str = "Admin",
) -> Dict[str, Any]:
    """
    Creates an invitation for a user.
    - Enforces Discord Admin verification for role 'admin'.
    - Configures OpenLDAP record.
    - Generates Authentik invitation link.
    - Attempts Discord DM dispatch.
    - Logs to #admin-logs.
    - Synchronizes #spielerliste live board.
    """
    clean_role = (role or "player").lower().strip()
    if clean_role not in ("admin", "mod", "player"):
        raise ValueError(f"Ungültige Rolle '{role}'. Erlaubt sind: player, mod, admin.")

    clean_discord = discord_name.strip()
    if not clean_discord:
        raise ValueError("Discord-Nutzername oder ID darf nicht leer sein.")

    # 1. Security Check: Admin role requires user to already be Discord Admin
    if clean_role == "admin":
        is_admin, admin_reason = check_is_discord_admin(clean_discord)
        if not is_admin:
            log_admin_event(
                title="🚨 Sicherheitswarnung: Admin-Einladung verweigert",
                description=f"Versuch, **{clean_discord}** als **admin** einzuladen, wurde abgewiesen.",
                color=COLOR_ERROR,
                fields=[
                    {"name": "Grund", "value": admin_reason, "inline": False},
                    {"name": "Initiiert von", "value": inviter, "inline": True},
                ],
                author_name=inviter,
            )
            raise PermissionError(
                f"SICHERHEITSSPERRE: Der Nutzer '{clean_discord}' besitzt keine Discord-Administrator-Rechte!\n"
                f"Grund: {admin_reason}\n"
                f"Voraussetzung: Nur bestehende Discord-Administratoren können als 'admin' eingeladen werden."
            )

    # 2. Resolve Minecraft IGN if provided
    canonical_ign = None
    mc_uuid = None
    if ign and ign.strip():
        raw_ign = ign.strip()
        if not validate_minecraft_ign(raw_ign):
            raise ValueError(f"Ungültiger Minecraft-Name '{raw_ign}'. 1-16 Zeichen (Buchstaben, Ziffern, Unterstriche).")
        try:
            canonical_ign, mc_uuid = get_player_profile(raw_ign)
        except Exception as e:
            raise ValueError(f"Minecraft IGN '{raw_ign}' konnte bei Mojang nicht aufgelöst werden: {e}")

    # 3. Sanitize UID and Email
    clean_username = (
        sanitize_username(username)
        if username
        else sanitize_username(clean_discord)
    )
    # Protection: Never overwrite or re-invite the master internal admin account 'tigimax' or 'akadmin'
    if clean_username.lower() in ("tigimax", "akadmin") and not username:
        clean_username = "tigimax_player"

    user_email = sanitize_email(clean_username)

    # 4. Configure OpenLDAP record
    discord_id = clean_discord if clean_discord.isdigit() else None
    ldap = LDAPClient()
    authentik = AuthentikClient()

    try:
        ldap.create_or_update_user(
            username=clean_username,
            discord_name=clean_discord,
            discord_id=discord_id,
            email=user_email,
            role=clean_role,
            minecraft_name=canonical_ign,
            minecraft_uuid=mc_uuid,
        )
    except Exception as e:
        logger.error(f"Failed to create LDAP entry for {clean_username}: {e}")
        raise RuntimeError(f"OpenLDAP-Fehler beim Anlegen des Nutzers: {e}")

    # 5. Reset any prior Authentik staging
    authentik.delete_user(clean_username)

    # 6. Generate Authentik Invitation Token
    fixed_data: Dict[str, Any] = {
        "username": clean_username,
        "email": user_email,
        "role": clean_role,
        "discord_name": clean_discord,
    }
    if discord_id:
        fixed_data["discord_id"] = discord_id
    if canonical_ign:
        fixed_data["minecraft_name"] = canonical_ign

    try:
        invite_link = authentik.create_invitation(
            name=f"invite-{clean_username}",
            fixed_data=fixed_data,
        )
    except Exception as e:
        logger.error(f"Failed to generate Authentik invitation for {clean_username}: {e}")
        raise RuntimeError(f"Authentik-Fehler bei der Token-Erstellung: {e}")

    if not invite_link:
        raise RuntimeError("Authentik hat keinen gültigen Einladungslink zurückgegeben.")

    # 7. Dispatch Discord DM
    dm_message = (
        f"🎅 **Einladung für Weihnachtsdorf 7** 🎄\n\n"
        f"Du wurdest von **{inviter}** als **{clean_role.capitalize()}** eingeladen!\n"
        f"Bitte schließe deine Registrierung über folgenden persönlichen Einladungslink ab:\n"
        f"👉 {invite_link}\n\n"
        f"📌 **Was dich erwartet:**\n"
        f"• Passwort & 2FA einrichten (zentraler SSO-Account für alle Portale)\n"
        f"• Angabe deines Minecraft Ingame-Namens (falls noch nicht hinterlegt)\n"
        f"• Nach Abschluss ist dein Profil startklar für den Server!\n\n"
        f"*Dieser Link ist einmalig gültig und verfällt nach 24 Stunden.*"
    )
    dm_sent = send_dm(clean_discord, dm_message)

    # 8. Dispatch Audit Event to #admin-logs
    log_admin_event(
        title="✉️ Manuelle Einladung erstellt",
        description=f"Einladung für **{clean_discord}** (`{clean_username}`) wurde generiert.",
        color=COLOR_SUCCESS if dm_sent else COLOR_WARNING,
        fields=[
            {"name": "Rolle", "value": f"`{clean_role}`", "inline": True},
            {"name": "Erstellt von", "value": inviter, "inline": True},
            {"name": "Minecraft IGN", "value": canonical_ign or "*(Bei Registrierung)*", "inline": True},
            {"name": "DM-Status", "value": "✅ Zugestellt" if dm_sent else "⚠️ Fehlgeschlagen (Link manuell bereitgestellt)", "inline": True},
            {"name": "Einladungslink (Backup)", "value": f"`{invite_link}`", "inline": False},
        ],
        author_name=inviter,
    )

    # 9. Synchronize live participant board
    try:
        sync_playerlist_rest(force=True)
    except Exception as e:
        logger.warning(f"Failed to update playerlist board: {e}")

    return {
        "success": True,
        "username": clean_username,
        "role": clean_role,
        "discord_name": clean_discord,
        "discord_id": discord_id,
        "minecraft_name": canonical_ign,
        "minecraft_uuid": mc_uuid,
        "invite_link": invite_link,
        "dm_sent": dm_sent,
    }
