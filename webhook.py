"""
FastAPI Webhook & Onboarding server for Weihnachtsdorf 7.
Coordinates:
- Onboarding completion (IGN validation & resolution, LDAP write-back, Planka provisioning, Minecraft LuckPerms/whitelist sync, Welcome DM)
- Authentik event notifications
- Privilege sync triggers
"""

import logging
from typing import Any, Dict, Optional
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.discord_bot import (
    get_player_welcome_message,
    get_welcome_message,
    send_dm,
)
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.mojang_client import get_player_profile
from wd_identity.planka_sync import PlankaSyncClient
from wd_identity.utils import (
    check_is_discord_admin,
    sanitize_email,
    sanitize_username,
    validate_minecraft_ign,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Weihnachtsdorf 7 Identity Webhook",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)


class OnboardingRequest(BaseModel):
    username: str
    minecraft_name: str
    discord_name: Optional[str] = None
    discord_id: Optional[str] = None
    role: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None


class AuthentikWebhookEvent(BaseModel):
    action: Optional[str] = None
    user: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "wd-identity",
        "version": "1.0.0",
    }


def process_onboarding(req: OnboardingRequest) -> Dict[str, Any]:
    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    planka = PlankaSyncClient()

    clean_user = sanitize_username(req.username)
    raw_ign = (req.minecraft_name or "").strip()
    if not validate_minecraft_ign(raw_ign):
        raise ValueError(f"Invalid Minecraft In-Game Name: '{raw_ign}'")

    # Fetch existing LDAP record to avoid losing Discord info or assigned role
    existing = ldap.get_user(clean_user)
    if not existing:
        existing = ldap.search_by_discord(req.discord_name or clean_user)

    clean_role = (
        (req.role or "").strip().lower()
        or (existing.get("role") if existing else None)
        or "player"
    )
    if clean_role not in ("admin", "mod", "player"):
        clean_role = "player"

    discord_tag = (
        (req.discord_name or "").strip()
        or (existing.get("discordName") if existing else None)
        or clean_user
    )
    discord_id = (
        (req.discord_id or "").strip()
        or (existing.get("discordId") if existing else None)
    )

    # Sicherheitsprüfung: Rolle 'admin' darf nur vergeben werden, wenn der Nutzer tatsächlich Discord-Admin ist
    if clean_role == "admin":
        is_admin, admin_reason = check_is_discord_admin(discord_id or discord_tag)
        if not is_admin:
            logger.warning(
                f"SICHERHEITSWARNUNG: Onboarding als 'admin' für '{clean_user}' ({discord_tag}) verweigert! "
                f"Nutzer ist kein Discord-Administrator ({admin_reason}). Rolle wird auf 'player' heruntergestuft."
            )
            clean_role = "player"
        else:
            logger.info(f"Admin-Onboarding für {clean_user} verifiziert: {admin_reason}")

    user_email = sanitize_email(clean_user, raw_email=req.email or (existing.get("mail") if existing else None))

    # 1. Resolve Mojang profile (canonical IGN and UUID)
    canonical_ign, mc_uuid = get_player_profile(raw_ign)
    logger.info(
        f"Resolved Minecraft profile for {raw_ign}: {canonical_ign} ({mc_uuid})"
    )

    # 2. Update OpenLDAP
    ldap.create_or_update_user(
        username=clean_user,
        discord_name=discord_tag,
        discord_id=discord_id,
        email=user_email,
        role=clean_role,
        minecraft_name=canonical_ign,
        minecraft_uuid=mc_uuid,
    )
    if req.password and req.password.strip():
        ldap.set_password(clean_user, req.password.strip())
    logger.info(f"Updated LDAP user {clean_user} with Minecraft & Discord metadata.")

    # 2.5 Direct Authentik Group Assignment (ensures staff web access without LDAP sync delays)
    ak = AuthentikClient()
    group_map = {"admin": "wd_admins", "mod": "wd_mods", "player": "wd_players"}
    ak_group = group_map.get(clean_role, "wd_players")
    ak.add_user_to_group(clean_user, ak_group)
    if req.username and req.username != clean_user:
        ak.add_user_to_group(req.username, ak_group)
    logger.info(f"Assigned user {clean_user} to Authentik group {ak_group}.")

    # 3. Provision in Planka (Staff only)
    planka_ok = None
    if clean_role in ("admin", "mod"):
        planka_ok = planka.sync_user(
            username=clean_user,
            email=user_email,
            name=clean_user.capitalize(),
            role=clean_role,
        )
        logger.info(f"Planka provisioning for {clean_user}: {'OK' if planka_ok else 'FAILED'}")
    else:
        logger.info(f"Skipping Planka provisioning for regular player {clean_user}.")

    # 4. Sync Minecraft whitelist and LuckPerms
    mc_ok = mc.set_player_role(canonical_ign, clean_role, mc_uuid=mc_uuid)
    logger.info(
        f"Minecraft server whitelist & LuckPerms role set for {canonical_ign} ({clean_role}): {'OK' if mc_ok else 'FAILED'}"
    )

    # 5. Dispatch Welcome DM via Discord
    if clean_role == "player":
        welcome_text = get_player_welcome_message(
            username=clean_user,
            ign=canonical_ign,
            is_launched=mc.is_project_launched(),
        )
    else:
        welcome_text = get_welcome_message(
            username=clean_user,
            role=clean_role,
            ign=canonical_ign,
        )
    dm_target = discord_id or discord_tag
    send_dm(dm_target, welcome_text)

    # 5.5 Dispatch Audit Log to #admin-logs
    try:
        from wd_identity.audit_logger import log_admin_event, COLOR_SUCCESS
        log_admin_event(
            title="🎉 Onboarding erfolgreich abgeschlossen",
            description=f"Spieler **{clean_user}** (`{canonical_ign}`) hat die Registrierung abgeschlossen und ist einsatzbereit!",
            color=COLOR_SUCCESS,
            fields=[
                {"name": "Benutzername (UID)", "value": f"`{clean_user}`", "inline": True},
                {"name": "Minecraft IGN", "value": f"`{canonical_ign}` ({mc_uuid})", "inline": True},
                {"name": "Discord", "value": f"<@{discord_id}>" if discord_id else f"`@{discord_tag}`", "inline": True},
                {"name": "Rolle", "value": f"`{clean_role}`", "inline": True},
                {"name": "Minecraft Server", "value": "✅ Whitelist & LuckPerms synchronisiert" if mc_ok else "⚠️ Sync-Warnung", "inline": True},
                {"name": "Planka Board", "value": "✅ Bereitgestellt" if planka_ok else "ℹ️ Übersprungen (Spieler)", "inline": True},
            ],
            author_name="Authentik Onboarding Webhook",
        )
    except Exception as e:
        logger.warning(f"Failed to post onboarding audit log: {e}")

    # 6. Synchronize Discord Playerlist
    try:
        from wd_identity.playerlist import sync_playerlist_rest
        sync_playerlist_rest(force=True)
    except Exception as e:
        logger.warning(f"Failed to update Discord playerlist on onboarding: {e}")

    return {
        "status": "success",
        "username": clean_user,
        "minecraft_name": canonical_ign,
        "minecraft_uuid": mc_uuid,
        "role": clean_role,
        "planka_synced": planka_ok,
        "minecraft_synced": mc_ok,
    }


@app.post("/api/onboarding/complete")
def complete_onboarding(req: OnboardingRequest):
    """
    Called after user completes enrollment (sets password, 2FA, and enters Minecraft IGN).
    """
    try:
        return process_onboarding(req)
    except ValueError as e:
        logger.warning(f"Validation error in onboarding for {req.username}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Error processing onboarding for {req.username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync")
def trigger_full_sync():
    """Triggers Minecraft server admin/mod synchronization."""
    mc = MinecraftSyncClient()
    report = mc.sync_all()
    try:
        from wd_identity.playerlist import sync_playerlist_rest
        sync_playerlist_rest(force=True)
    except Exception as e:
        logger.warning(f"Failed to update Discord playerlist during sync: {e}")
    return report


@app.post("/api/webhook/authentik")
def authentik_webhook(
    event: AuthentikWebhookEvent, background_tasks: BackgroundTasks
):
    """Webhook listener for Authentik event notifications."""
    logger.info(f"Received Authentik webhook: action={event.action}")
    if event.user and "username" in event.user:
        username = event.user["username"]
        # Trigger Minecraft sync and LDAP check in background
        background_tasks.add_task(trigger_full_sync)
        return {"status": "accepted", "user": username}
    return {"status": "ignored"}


def run_server():
    import uvicorn

    uvicorn.run(
        app,
        host=settings.WEBHOOK_HOST,
        port=settings.WEBHOOK_PORT,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_server()
