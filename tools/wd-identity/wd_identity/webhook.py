"""
FastAPI Webhook & Onboarding server for Weihnachtsdorf 7.
Coordinates:
- Onboarding completion (IGN validation & resolution, LDAP write-back, Planka provisioning, Minecraft LuckPerms/whitelist sync, Welcome DM)
- Authentik event notifications
- Privilege sync triggers
"""

import hmac
import logging
from typing import Any, Dict, Optional
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
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
    check_is_discord_mod,
    sanitize_email,
    sanitize_username,
    validate_minecraft_ign,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Weihnachtsdorf 7 Identity Webhook",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)


def verify_webhook_auth(
    authorization: Optional[str] = Header(None),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
):
    """
    Enforces webhook secret authentication when WEBHOOK_SECRET is configured.
    Accepts secret via X-Webhook-Secret header or Authorization: Bearer <secret>.
    """
    expected_secret = (getattr(settings, "WEBHOOK_SECRET", "") or "").strip()
    if not expected_secret:
        logger.warning(
            "SECURITY WARNING: WEBHOOK_SECRET is not configured. Webhook endpoints are open to unauthenticated requests!"
        )
        return

    token = None
    if x_webhook_secret:
        token = x_webhook_secret.strip()
    elif authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        elif len(parts) == 1:
            token = parts[0].strip()

    if not token or not hmac.compare_digest(token, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook authentication secret.",
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


@app.get("/")
@app.get("/health")
@app.get("/status")
@app.get("/api/health")
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


@app.post("/api/mfa/auto-dispatch", dependencies=[Depends(verify_webhook_auth)])
def mfa_auto_dispatch(req: MFADispatchRequest):
    """
    Called automatically by Authentik during the MFA validation stage.
    Generates a 6-digit static token in Authentik and dispatches it via Discord DM
    to the player. Staff members (admin/mod) are skipped so they use TOTP.
    """
    clean_user = sanitize_username(req.username)
    if not clean_user or not req.username or not req.username.strip():
        return {"status": "error", "detail": "Empty username"}

    ldap = LDAPClient()
    ak = AuthentikClient()

    # 1. Fetch user from LDAP & Authentik to verify role and ban status
    user = ldap.get_user(clean_user)
    ak_user = ak.get_user_by_username(clean_user)
    ak_attrs = ak_user.get("attributes", {}) if isinstance(ak_user, dict) else {}

    # Check ban status in LDAP and Authentik
    if (user and isinstance(user, dict) and user.get("accountStatus") == "banned") or ak_attrs.get("is_banned") or ak_attrs.get("accountStatus") == "banned":
        logger.warning(f"Refusing MFA auto-dispatch for banned user '{clean_user}'")
        return {"status": "error", "detail": "Account is banned"}

    # Strict check: Staff members (admin/mod) or accounts with TOTP MUST use TOTP
    user_dn = (
        user.get("dn")
        if (user and isinstance(user, dict))
        else f"uid={clean_user},{getattr(ldap, 'user_ou', 'ou=Users,dc=ldap-01,dc=net')}"
    )

    admin_dns = set()
    mod_dns = set()
    try:
        admin_group = getattr(ldap, "admin_group_dn", None) or getattr(ldap, "admin_group", None)
        if admin_group:
            admin_dns = set(ldap.get_group_members(admin_group))
        mod_group = getattr(ldap, "mod_group_dn", None) or getattr(ldap, "mod_group", None)
        if mod_group:
            mod_dns = set(ldap.get_group_members(mod_group))
    except Exception as e:
        logger.debug(f"Could not fetch LDAP group memberships in mfa_auto_dispatch: {e}")

    user_role = (
        (user.get("role") if (user and isinstance(user, dict)) else None)
        or ak_attrs.get("role")
        or ""
    )
    if isinstance(user_role, str):
        user_role = user_role.strip().lower()

    is_staff = (
        (user_dn in admin_dns or user_dn in mod_dns)
        or (user_role in ("admin", "mod"))
        or ak.has_totp_device(clean_user)
    )

    if is_staff:
        logger.info(f"MFA auto-dispatch skipped for staff/TOTP user '{clean_user}'")
        return {"status": "skipped", "reason": "staff_or_totp"}

    # 2. Resolve Discord ID strictly:
    # Always prioritize bound identity in LDAP or Authentik. Never allow caller to override bound Discord ID!
    bound_discord_id = ""
    if user and isinstance(user, dict):
        bound_discord_id = str(user.get("discordId") or "").strip()
    if not bound_discord_id and isinstance(ak_attrs, dict):
        bound_discord_id = str(ak_attrs.get("discord_id") or "").strip()

    if bound_discord_id:
        discord_id = bound_discord_id
    elif user is None and req.discord_id and req.discord_id.strip():
        # Only during initial pre-enrollment before LDAP user exists
        discord_id = req.discord_id.strip()
    else:
        logger.warning(f"Cannot auto-dispatch MFA code for '{clean_user}': No bound Discord ID found")
        return {"status": "error", "detail": "No bound Discord ID linked"}

    # 3. Generate static token in Authentik
    token = ak.generate_static_token_for_user(clean_user)
    if not token:
        logger.error(f"Failed to generate static MFA token for '{clean_user}'")
        return {"status": "error", "detail": "Token generation failed"}

    # 4. Dispatch Discord DM
    dm_text = (
        "🔑 **Dein Login-Code für Authentik**\n\n"
        f"Dein 6-stelliger Einmal-Code lautet:\n"
        f"# `{token}`\n\n"
        "Gib diesen Code jetzt im Browser ein, um deine Anmeldung abzuschließen.\n"
        "*Hinweis: Dieser Code ist für deinen aktuellen Login-Vorgang gültig.*"
    )
    dm_sent = send_dm(str(discord_id), dm_text)
    if dm_sent:
        logger.info(f"Automatically dispatched login code via DM to {clean_user} (Discord: {discord_id})")
        return {"status": "success", "dispatched": True}
    else:
        logger.warning(f"Failed to deliver Discord DM to {discord_id} for user {clean_user}")
        return {"status": "error", "detail": "Discord DM delivery failed"}


def process_onboarding(req: OnboardingRequest) -> Dict[str, Any]:
    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    planka = PlankaSyncClient()
    ak = AuthentikClient()

    raw_username = (req.username or "").strip()
    clean_user = sanitize_username(raw_username)
    if not raw_username or not clean_user:
        return {
            "status": "error",
            "detail": "Invalid or empty username provided.",
        }

    # Protection: Never overwrite or map event participant onto internal admin 'tigimax' or 'akadmin'
    if clean_user.lower() in ("tigimax", "akadmin"):
        clean_user = "tigimax_player"

    raw_ign = (req.minecraft_name or "").strip()
    if not validate_minecraft_ign(raw_ign):
        raise ValueError(f"Invalid Minecraft In-Game Name: '{raw_ign}'")

    # Fetch existing LDAP record strictly by username
    existing = ldap.get_user(clean_user)

    # Fetch Authentik user attributes if available (trusted source from invitation flow)
    ak_user = ak.get_user_by_username(clean_user)
    ak_attrs = ak_user.get("attributes", {}) if isinstance(ak_user, dict) else {}

    # Check ban status in LDAP and Authentik
    if (existing and isinstance(existing, dict) and existing.get("accountStatus") == "banned") or ak_attrs.get("is_banned") or ak_attrs.get("accountStatus") == "banned":
        logger.warning(
            f"SICHERHEITSWARNUNG: Onboarding verweigert für gesperrten Nutzer '{clean_user}'!"
        )
        return {
            "status": "error",
            "username": clean_user,
            "detail": "Account is banned",
        }

    # Trusted Discord ID: prioritize existing LDAP or Authentik invitation attributes
    trusted_discord_id = (existing.get("discordId") if isinstance(existing, dict) else None) or ak_attrs.get("discord_id")
    if trusted_discord_id:
        discord_id = str(trusted_discord_id).strip()
    else:
        candidate_discord_id = (req.discord_id or "").strip()
        if candidate_discord_id:
            # Check if candidate Discord ID is already bound to another LDAP user
            other_user = ldap.search_by_discord_id(candidate_discord_id)
            if other_user and isinstance(other_user, dict) and other_user.get("uid") != clean_user:
                logger.warning(
                    f"SICHERHEITSWARNUNG: Discord ID '{candidate_discord_id}' ist bereits an Nutzer "
                    f"'{other_user.get('uid')}' gebunden! Bindung für '{clean_user}' abgewiesen."
                )
                discord_id = ""
            else:
                discord_id = candidate_discord_id
        else:
            discord_id = ""

    discord_tag = (
        (existing.get("discordName") if isinstance(existing, dict) else None)
        or ak_attrs.get("discord_name")
        or (req.discord_name or "").strip()
        or clean_user
    )

    requested_role = (req.role or "").strip().lower()

    # Determine pre-authorization from LDAP groups and Authentik invite attributes
    admin_dns = set()
    mod_dns = set()
    try:
        admin_group = getattr(ldap, "admin_group_dn", None) or getattr(ldap, "admin_group", None)
        if admin_group:
            admin_dns = set(ldap.get_group_members(admin_group))
        mod_group = getattr(ldap, "mod_group_dn", None) or getattr(ldap, "mod_group", None)
        if mod_group:
            mod_dns = set(ldap.get_group_members(mod_group))
    except Exception as e:
        logger.debug(f"Could not fetch LDAP group memberships in process_onboarding: {e}")

    user_dn = (
        existing.get("dn")
        if (existing and isinstance(existing, dict))
        else f"uid={clean_user},{getattr(ldap, 'user_ou', 'ou=Users,dc=ldap-01,dc=net')}"
    )

    existing_ldap_role = existing.get("role") if isinstance(existing, dict) else None
    ak_role = ak_attrs.get("role")

    is_preauthorized_admin = (
        (user_dn in admin_dns)
        or (existing_ldap_role == "admin")
        or (str(ak_role).strip().lower() == "admin")
    )
    is_preauthorized_mod = (
        (user_dn in mod_dns)
        or (existing_ldap_role in ("admin", "mod"))
        or (str(ak_role).strip().lower() in ("admin", "mod"))
    )

    # Staff roles (admin / mod) can ONLY be granted if authorized by a trusted source (existing LDAP or Authentik invitation)
    clean_role = "player"
    if requested_role == "admin":
        if not is_preauthorized_admin:
            logger.warning(
                f"SICHERHEITSWARNUNG: Onboarding als 'admin' für '{clean_user}' verweigert! "
                f"Weder LDAP noch Authentik-Einladung autorisieren die Admin-Rolle. Rolle wird auf 'player' heruntergestuft."
            )
            clean_role = "player"
        elif not trusted_discord_id or not str(trusted_discord_id).strip().isdigit():
            logger.warning(
                f"SICHERHEITSWARNUNG: Admin-Onboarding für '{clean_user}' verweigert! Keine vertrauenswürdige numerische Discord-ID gebunden."
            )
            clean_role = "player"
        else:
            # Verify against Discord API strictly using trusted immutable snowflake
            is_admin, admin_reason = check_is_discord_admin(str(trusted_discord_id).strip())
            if not is_admin:
                logger.warning(
                    f"SICHERHEITSWARNUNG: Onboarding als 'admin' für '{clean_user}' verweigert! "
                    f"Nutzer ist kein Discord-Administrator ({admin_reason}). Rolle wird auf 'player' heruntergestuft."
                )
                clean_role = "player"
            else:
                logger.info(f"Admin-Onboarding für {clean_user} verifiziert: {admin_reason}")
                clean_role = "admin"

    elif requested_role == "mod":
        if not is_preauthorized_mod:
            logger.warning(
                f"SICHERHEITSWARNUNG: Onboarding als 'mod' für '{clean_user}' verweigert! "
                f"Weder LDAP noch Authentik-Einladung autorisieren Mod-Rechte. Rolle wird auf 'player' heruntergestuft."
            )
            clean_role = "player"
        elif trusted_discord_id and str(trusted_discord_id).strip().isdigit():
            is_mod, mod_reason = check_is_discord_mod(str(trusted_discord_id).strip())
            if not is_mod:
                logger.warning(
                    f"SICHERHEITSWARNUNG: Onboarding als 'mod' für '{clean_user}' verweigert! "
                    f"Nutzer ist kein Discord-Moderator ({mod_reason}). Rolle wird auf 'player' heruntergestuft."
                )
                clean_role = "player"
            else:
                logger.info(f"Mod-Onboarding für {clean_user} verifiziert: {mod_reason}")
                clean_role = "mod"
        else:
            clean_role = "mod"
    else:
        if is_preauthorized_admin and not requested_role:
            clean_role = "admin"
        elif is_preauthorized_mod and not requested_role:
            clean_role = "mod"
        else:
            clean_role = "player"

    user_email = sanitize_email(clean_user, raw_email=req.email or (existing.get("mail") if isinstance(existing, dict) else None))

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
        if existing:
            logger.warning(
                f"SICHERHEITSHINWEIS: Passwort-Setzen für bestehenden LDAP-Nutzer '{clean_user}' bei Onboarding ignoriert."
            )
        else:
            ldap.set_password(clean_user, req.password.strip())
    logger.info(f"Updated LDAP user {clean_user} with Minecraft & Discord metadata.")

    # 2.5 Direct Authentik Group Assignment & Old Group Cleanup
    group_map = {"admin": "wd_admins", "mod": "wd_mods", "player": "wd_players"}
    ak_group = group_map.get(clean_role, "wd_players")
    for old_group in {"wd_admins", "wd_mods", "wd_players"} - {ak_group}:
        ak.remove_user_from_group(clean_user, old_group)
    ak.add_user_to_group(clean_user, ak_group)
    if req.username and req.username != clean_user and req.username.lower() not in ("tigimax", "akadmin"):
        for old_group in {"wd_admins", "wd_mods", "wd_players"} - {ak_group}:
            ak.remove_user_from_group(req.username, old_group)
        ak.add_user_to_group(req.username, ak_group)
    logger.info(f"Assigned user {clean_user} to Authentik group {ak_group} and cleaned up old groups.")

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


@app.post("/api/onboarding/complete", dependencies=[Depends(verify_webhook_auth)])
def complete_onboarding(req: OnboardingRequest):
    """
    Called after user completes enrollment (sets password, 2FA, and enters Minecraft IGN).
    """
    try:
        report = process_onboarding(req)
        if isinstance(report, dict) and report.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=report.get("detail", "Onboarding rejected"),
            )
        return report
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error in onboarding for {req.username}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Error processing onboarding for {req.username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync", dependencies=[Depends(verify_webhook_auth)])
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


@app.post("/api/webhook/authentik", dependencies=[Depends(verify_webhook_auth)])
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
