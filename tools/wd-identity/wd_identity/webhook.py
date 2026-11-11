"""
FastAPI Webhook & Onboarding server for Weihnachtsdorf 7.
Coordinates:
- Onboarding completion (IGN validation & resolution, LDAP write-back, Planka provisioning, Minecraft LuckPerms/whitelist sync, Welcome DM)
- Authentik event notifications
- Privilege sync triggers
"""

import logging
import threading
import time
from typing import Any, Dict, Optional, Set
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
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
    resolve_discord_user,
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


class MFADispatchRequest(BaseModel):
    username: str
    discord_id: Optional[str] = None


class WebSetupCompleteRequest(BaseModel):
    username: str
    password: str
    code: Optional[str] = None


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "wd-identity",
        "version": "1.0.0",
    }


@app.get("/api/project/status")
def get_project_status():
    """
    Returns current project launch status.
    Used by monitoring and as a secondary verification for Authentik's wd-bluemap-access-policy.
    """
    mc = MinecraftSyncClient()
    is_launched = mc.is_project_launched()
    return {
        "launched": is_launched,
        "mode": "live" if is_launched else "pre-launch",
        "bluemap_access": "unlocked" if is_launched else "locked_for_players",
    }


_mfa_cooldown_tracker: Dict[str, float] = {}
_mfa_lock = threading.Lock()


def reset_mfa_cooldown_tracker() -> None:
    """Helper for unittests to clear cooldown state."""
    with _mfa_lock:
        _mfa_cooldown_tracker.clear()


def _clean_discord_id(val: Any) -> str:
    """Safely extracts and validates a Discord snowflake ID, rejecting null/none strings."""
    if val is None:
        return ""
    cleaned = str(val).strip()
    if cleaned.lower() in ("none", "null", ""):
        return ""
    return cleaned


@app.post("/api/mfa/auto-dispatch")
def mfa_auto_dispatch(req: MFADispatchRequest):
    """
    Called automatically by Authentik during the MFA validation stage.
    Generates a 6-digit static token in Authentik and dispatches it via Discord DM
    to the player. Staff members (admin/mod) are skipped so they use TOTP.
    Enforces a 30-second cooldown per user to prevent duplicate DMs.
    Thread-safe against concurrent duplicate requests.
    """
    # 0. Reject empty or whitespace-only username before sanitization
    raw_user = (req.username or "").strip()
    if not raw_user:
        return {"status": "error", "detail": "Empty username"}

    clean_user = sanitize_username(raw_user)
    if not clean_user or (clean_user == "user" and raw_user.lower() != "user"):
        return {"status": "error", "detail": "Empty username"}

    now = time.time()
    user_key = clean_user.lower()

    # 1. Fast-path deduplication cooldown check (30 seconds)
    with _mfa_lock:
        last_dispatched = max(
            _mfa_cooldown_tracker.get(clean_user, 0.0),
            _mfa_cooldown_tracker.get(user_key, 0.0),
        )
        if now - last_dispatched < 30.0:
            logger.info(f"MFA auto-dispatch skipped for '{clean_user}': Cooldown active (<30s)")
            return {"status": "skipped", "reason": "cooldown_active"}

    # 2. Skip internal accounts immediately
    if user_key in ("tigimax", "akadmin"):
        logger.info(f"MFA auto-dispatch skipped for internal account '{clean_user}'")
        return {"status": "skipped", "reason": "staff_or_totp"}

    ldap = LDAPClient()
    ak = AuthentikClient()

    # 3. Check Authentik user: attributes, superuser status, and staff groups
    ak_user = ak.get_user_by_username(clean_user)
    if isinstance(ak_user, dict):
        ak_attrs = ak_user.get("attributes") or {}
        if isinstance(ak_attrs, dict):
            role_attr = str(ak_attrs.get("role") or "").strip().lower()
            mfa_attr = str(ak_attrs.get("mfa_method") or "").strip().lower()
            if role_attr in ("admin", "mod") or mfa_attr == "totp":
                logger.info(f"MFA auto-dispatch skipped for admin/totp user '{clean_user}' (from ak attributes)")
                return {"status": "skipped", "reason": "staff_or_totp"}

        if ak_user.get("is_superuser") is True:
            logger.info(f"MFA auto-dispatch skipped for superuser '{clean_user}'")
            return {"status": "skipped", "reason": "staff_or_totp"}

        ak_groups: Set[str] = set()
        groups_obj = ak_user.get("groups_obj")
        if isinstance(groups_obj, list):
            for g in groups_obj:
                if isinstance(g, dict) and g.get("name"):
                    ak_groups.add(str(g["name"]).strip().lower())
                elif isinstance(g, str):
                    ak_groups.add(g.strip().lower())
        groups = ak_user.get("groups")
        if isinstance(groups, list):
            for g in groups:
                if isinstance(g, str):
                    ak_groups.add(g.strip().lower())

        staff_groups = {"superadmins", "wd_admins", "wd_mods", "authentik admins"}
        if ak_groups.intersection(staff_groups):
            logger.info(f"MFA auto-dispatch skipped for staff user '{clean_user}' in groups {ak_groups}")
            return {"status": "skipped", "reason": "staff_or_totp"}

    # 4. Check TOTP device in Authentik
    if ak.has_totp_device(clean_user):
        logger.info(f"MFA auto-dispatch skipped for user '{clean_user}': Confirmed TOTP device exists")
        return {"status": "skipped", "reason": "staff_or_totp"}

    # 5. Fetch user from LDAP to verify role and ban status
    user = ldap.get_user(clean_user)
    if isinstance(user, dict):
        role = str(user.get("role") or "").strip().lower()
        if role in ("admin", "mod"):
            logger.info(f"MFA auto-dispatch skipped for staff user '{clean_user}' (LDAP role: {role})")
            return {"status": "skipped", "reason": "staff_or_totp"}

        if user.get("accountStatus") == "banned":
            logger.warning(f"Refusing MFA auto-dispatch for banned user '{clean_user}'")
            return {"status": "error", "detail": "Account is banned"}

    # 6. Resolve Discord ID
    discord_id = _clean_discord_id(req.discord_id)
    if not discord_id and isinstance(user, dict):
        discord_id = _clean_discord_id(user.get("discordId"))
    if not discord_id and isinstance(ak_user, dict) and isinstance(ak_user.get("attributes"), dict):
        discord_id = _clean_discord_id(ak_user["attributes"].get("discord_id"))

    if not discord_id:
        logger.warning(f"Cannot auto-dispatch MFA code for '{clean_user}': No Discord ID found")
        return {"status": "error", "detail": "No Discord ID linked"}

    # 7. Atomic Cooldown Reservation & In-Flight Gate
    now = time.time()
    with _mfa_lock:
        last_dispatched = max(
            _mfa_cooldown_tracker.get(clean_user, 0.0),
            _mfa_cooldown_tracker.get(user_key, 0.0),
        )
        if now - last_dispatched < 30.0:
            logger.info(f"MFA auto-dispatch skipped for '{clean_user}': Cooldown active (<30s)")
            return {"status": "skipped", "reason": "cooldown_active"}

        # Store previous state for rollback if downstream dispatch fails
        prev_user = _mfa_cooldown_tracker.get(clean_user)
        prev_key = _mfa_cooldown_tracker.get(user_key)
        _mfa_cooldown_tracker[clean_user] = now
        _mfa_cooldown_tracker[user_key] = now

        # Opportunistic eviction of stale records (>300s) if tracker grows large
        if len(_mfa_cooldown_tracker) > 1000:
            stale_keys = [k for k, v in _mfa_cooldown_tracker.items() if now - v > 300.0]
            for k in stale_keys:
                _mfa_cooldown_tracker.pop(k, None)

    def _rollback_reservation():
        with _mfa_lock:
            if prev_user is not None:
                _mfa_cooldown_tracker[clean_user] = prev_user
            else:
                _mfa_cooldown_tracker.pop(clean_user, None)
            if prev_key is not None:
                _mfa_cooldown_tracker[user_key] = prev_key
            else:
                _mfa_cooldown_tracker.pop(user_key, None)

    # 8. Generate static token in Authentik
    token = ak.generate_static_token_for_user(clean_user)
    if not token:
        logger.error(f"Failed to generate static MFA token for '{clean_user}'")
        _rollback_reservation()
        return {"status": "error", "detail": "Token generation failed"}

    # 9. Dispatch Discord DM
    dm_text = (
        "🔑 **Dein Login-Code für Authentik**\n\n"
        f"Dein 6-stelliger Einmal-Code lautet:\n"
        f"# `{token}`\n\n"
        "Gib diesen Code jetzt im Browser ein, um deine Anmeldung abzuschließen.\n"
        "*Hinweis: Dieser Code ist für deinen aktuellen Login-Vorgang gültig.*"
    )
    try:
        dm_sent = send_dm(str(discord_id), dm_text)
    except Exception as e:
        logger.exception(f"Unexpected exception delivering Discord DM to {discord_id} for user {clean_user}: {e}")
        _rollback_reservation()
        return {"status": "error", "detail": "Discord DM delivery failed"}

    if dm_sent:
        finish_time = time.time()
        with _mfa_lock:
            _mfa_cooldown_tracker[clean_user] = finish_time
            _mfa_cooldown_tracker[user_key] = finish_time
        logger.info(f"Automatically dispatched login code via DM to {clean_user} (Discord: {discord_id})")
        return {"status": "success", "dispatched": True}
    else:
        logger.warning(f"Failed to deliver Discord DM to {discord_id} for user {clean_user}")
        _rollback_reservation()
        return {"status": "error", "detail": "Discord DM delivery failed"}


def process_onboarding(req: OnboardingRequest) -> Dict[str, Any]:
    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    planka = PlankaSyncClient()

    clean_user = sanitize_username(req.username)
    # Protection: Never overwrite or map event participant onto internal admin 'tigimax' or 'akadmin'
    if clean_user.lower() in ("tigimax", "akadmin"):
        clean_user = "tigimax_player"

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
    if req.username and req.username != clean_user and req.username.lower() not in ("tigimax", "akadmin"):
        ak.add_user_to_group(req.username, ak_group)
    logger.info(f"Assigned user {clean_user} to Authentik group {ak_group}.")

    # Provision static MFA device for regular players (enables Discord bot one-time login codes)
    if clean_role == "player":
        ak.ensure_player_mfa_device(clean_user)
        logger.info(f"Ensured static MFA device for player {clean_user}.")

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

    # 5. Dispatch Welcome DM via Discord (address user with Discord name, not LDAP username)
    display_user = discord_tag
    if not display_user or display_user == clean_user or display_user.isdigit():
        try:
            _, resolved_name = resolve_discord_user(discord_id or discord_tag)
            if resolved_name and not resolved_name.isdigit():
                display_user = resolved_name
            elif existing and existing.get("discordName"):
                display_user = existing.get("discordName")
            else:
                display_user = clean_user
        except Exception:
            display_user = clean_user

    if clean_role == "player":
        welcome_text = get_player_welcome_message(
            username=display_user,
            ign=canonical_ign,
            is_launched=mc.is_project_launched(),
        )
    else:
        welcome_text = get_welcome_message(
            username=display_user,
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


@app.post("/api/web-setup/complete")
def complete_web_setup(req: WebSetupCompleteRequest):
    """
    Self-service web password setup endpoint for pre-provisioned players.
    1. Validates username and password (min 8 chars, non-empty/whitespace).
    2. Verifies 6-digit static token unless code == 'flow_verified'.
    3. Updates OpenLDAP password.
    4. Updates Authentik password.
    5. Clears Authentik 'needs_web_setup' attribute to False.
    """
    raw_user = (req.username or "").strip()
    if not raw_user:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Empty username"},
        )

    clean_user = sanitize_username(raw_user)
    if not clean_user or (clean_user == "user" and raw_user.lower() != "user"):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Invalid username"},
        )

    # Protect internal admin accounts from tampering
    if clean_user.lower() in ("tigimax", "akadmin"):
        clean_user = "tigimax_player"

    raw_pw = req.password or ""
    if not raw_pw or not raw_pw.strip():
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "detail": "Password cannot be empty or whitespace",
            },
        )

    ak = AuthentikClient()

    # Code verification check
    raw_code = (req.code or "").strip()
    if not raw_code:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Verification code is required"},
        )
    if raw_code != "flow_verified":
        if not ak.verify_static_token_for_user(clean_user, raw_code):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "detail": "Invalid or expired verification code"},
            )

    # Update OpenLDAP password
    ldap = LDAPClient()
    try:
        ldap_ok = ldap.set_password(clean_user, req.password)
    except Exception as e:
        logger.error(f"Exception updating OpenLDAP password for user '{clean_user}': {e}")
        ldap_ok = False

    if not ldap_ok:
        logger.error(f"Failed to update OpenLDAP password for user '{clean_user}'")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Failed to update LDAP password"},
        )

    # Update Authentik password
    ak_pw_ok = ak.set_user_password(clean_user, req.password)
    if not ak_pw_ok:
        logger.warning(f"Failed to update Authentik password for user '{clean_user}'")

    # Update Authentik needs_web_setup flag
    try:
        if not ak.set_needs_web_setup(clean_user, False):
            logger.warning(f"Failed to clear needs_web_setup for user '{clean_user}' in Authentik")
    except Exception as e:
        logger.warning(f"Exception setting needs_web_setup for user '{clean_user}': {e}")

    # Send confirmation DM with BlueMap link
    try:
        user_entry = ldap.get_user(clean_user)
        discord_target = (user_entry.get("discordId") or user_entry.get("discordName")) if user_entry else None
        if discord_target:
            send_dm(
                str(discord_target),
                f"✅ **Dein Web-Passwort wurde erfolgreich gespeichert!**\n\n"
                f"Du kannst dich ab sofort jederzeit auf der Live-Karte und allen Spieler-Diensten anmelden:\n"
                f"🗺️ **BlueMap Live-Karte:** 👉 https://map.tigimax.mooo.com/\n"
                f"👤 **Anmeldename:** `{ user_entry.get('discordName') or clean_user }` (oder `{ user_entry.get('minecraftName') or clean_user }`)",
            )
    except Exception as e:
        logger.warning(f"Failed to send web-setup confirmation DM for '{clean_user}': {e}")

    try:
        from wd_identity.audit_logger import log_admin_event, COLOR_SUCCESS
        log_admin_event(
            title="🔑 Web-Setup abgeschlossen",
            description=f"Spieler **{clean_user}** hat das Web-Passwort-Setup erfolgreich abgeschlossen.",
            color=COLOR_SUCCESS,
            fields=[
                {"name": "Benutzername (UID)", "value": f"`{clean_user}`", "inline": True},
                {"name": "Status", "value": "✅ Passwort gesetzt & Web-Setup abgeschlossen", "inline": True},
            ],
            author_name="Authentik Web Setup Webhook",
        )
    except Exception as e:
        logger.warning(f"Failed to post web-setup audit log: {e}")

    return {
        "status": "success",
        "username": clean_user,
        "needs_web_setup": False,
    }


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
