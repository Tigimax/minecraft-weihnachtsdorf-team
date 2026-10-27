"""
FastAPI Webhook & Onboarding server for Weihnachtsdorf 7.
Coordinates:
- Onboarding completion (IGN resolution, LDAP write-back, Planka provisioning, Minecraft LuckPerms/whitelist sync, Welcome DM)
- Authentik event notifications
- Privilege sync triggers
"""

import logging
from typing import Any, Dict, Optional
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.discord_bot import get_welcome_message, send_dm
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.mojang_client import get_player_profile
from wd_identity.planka_sync import PlankaSyncClient

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
    role: Optional[str] = "admin"
    email: Optional[str] = None


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

    username = req.username.strip().lower()
    role = (req.role or "admin").strip().lower()
    discord_tag = req.discord_name or username

    # 1. Resolve Mojang profile (canonical IGN and UUID)
    canonical_ign, mc_uuid = get_player_profile(req.minecraft_name)
    logger.info(
        f"Resolved Minecraft profile for {req.minecraft_name}: {canonical_ign} ({mc_uuid})"
    )

    # 2. Update OpenLDAP
    ldap.create_or_update_user(
        username=username,
        discord_name=discord_tag,
        discord_id=req.discord_id,
        email=req.email,
        role=role,
        minecraft_name=canonical_ign,
        minecraft_uuid=mc_uuid,
    )
    logger.info(f"Updated LDAP user {username} with Minecraft & Discord metadata.")

    # 3. Provision in Planka
    planka_ok = planka.sync_user(
        username=username,
        email=req.email or f"{username}@tigimax.mooo.com",
        name=username.capitalize(),
        role=role,
    )
    logger.info(f"Planka provisioning for {username}: {'OK' if planka_ok else 'FAILED'}")

    # 4. Sync Minecraft whitelist and LuckPerms
    mc_ok = mc.set_player_role(canonical_ign, role, mc_uuid=mc_uuid)
    logger.info(
        f"Minecraft server whitelist & LuckPerms role set for {canonical_ign} ({role}): {'OK' if mc_ok else 'FAILED'}"
    )

    # 5. Dispatch Welcome DM via Discord
    welcome_text = get_welcome_message(
        username=username,
        role=role,
        ign=canonical_ign,
    )
    dm_target = req.discord_id or discord_tag
    send_dm(dm_target, welcome_text)

    return {
        "status": "success",
        "username": username,
        "minecraft_name": canonical_ign,
        "minecraft_uuid": mc_uuid,
        "role": role,
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
    except Exception as e:
        logger.exception(f"Error processing onboarding for {req.username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync")
def trigger_full_sync():
    """Triggers Minecraft server admin/mod synchronization."""
    mc = MinecraftSyncClient()
    report = mc.sync_all()
    return report


@app.post("/api/webhook/authentik")
def authentik_webhook(
    event: AuthentikWebhookEvent, background_tasks: BackgroundTasks
):
    """Webhook listener for Authentik event notifications."""
    logger.info(f"Received Authentik webhook: {event.action}")
    if event.user and "username" in event.user:
        username = event.user["username"]
        # Trigger sync in background
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
