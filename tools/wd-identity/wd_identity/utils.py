"""
Utility and validation functions for wd-identity.
Handles input sanitization, Minecraft IGN validation, and SQL/LDAP safety.
"""

import re
import unicodedata
from typing import Optional, Tuple


UMLAUT_MAP = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "ae",
    "Ö": "oe",
    "Ü": "ue",
}


def sanitize_username(raw: str) -> str:
    """
    Sanitizes raw username (from Discord, CLI, or web) into a valid LDAP UID.
    - Transliterates German umlauts (ä->ae, etc.)
    - Removes accents and non-ASCII characters
    - Normalizes separators (#, spaces, dots) to underscores
    - Restricts to [a-z0-9._-]
    """
    if not raw:
        return "user"

    text = raw.strip()
    for char, replacement in UMLAUT_MAP.items():
        text = text.replace(char, replacement)

    # Decompose unicode accents
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")

    # Lowercase and replace non-alphanumeric symbols
    cleaned = ascii_text.lower()
    cleaned = re.sub(r"[\s#:/\\,@+]+", "_", cleaned)
    cleaned = re.sub(r"[^a-z0-9._-]", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")

    if not cleaned:
        return "user"
    return cleaned


def sanitize_email(username: str, domain: str = "tigimax.mooo.com", raw_email: Optional[str] = None) -> str:
    """
    Returns a valid ASCII email address conforming to LDAP IA5String syntax.
    If raw_email is provided, validates that it is pure ASCII; otherwise falls back to {clean_user}@{domain}.
    """
    clean_user = sanitize_username(username)
    if raw_email:
        raw_email = raw_email.strip().lower()
        # Check if pure ASCII and valid format
        if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", raw_email):
            try:
                raw_email.encode("ascii")
                return raw_email
            except UnicodeEncodeError:
                pass
    return f"{clean_user}@{domain}"


def validate_minecraft_ign(ign: str) -> bool:
    """
    Validates Minecraft In-Game Name.
    Strictly permits only 1-16 alphanumeric characters and underscores.
    Rejects spaces, newlines, semicolons, and special characters.
    """
    if not ign or not isinstance(ign, str):
        return False
    return bool(re.match(r"^[a-zA-Z0-9_]{1,16}$", ign.strip()))


def escape_sql_literal(val: str) -> str:
    """
    Safely escapes single quotes for SQL string literals.
    """
    if val is None:
        return ""
    return str(val).replace("'", "''")


def _discord_rest_request(
    url: str,
    headers: dict,
    data: Optional[bytes] = None,
    method: str = "GET",
    timeout: int = 10,
) -> Tuple[int, bytes]:
    """
    Executes a Discord REST API request with a single retry on HTTP 429
    when Retry-After <= 5 seconds.
    """
    import json
    import time
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200), resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after_raw = None
            try:
                body = e.read()
                parsed = json.loads(body.decode("utf-8"))
                retry_after_raw = parsed.get("retry_after")
            except Exception:
                pass
            if retry_after_raw is None and e.headers:
                retry_after_raw = e.headers.get("Retry-After")
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = None
            if retry_after is not None and 0 <= retry_after <= 5.0:
                time.sleep(retry_after)
                retry_req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(retry_req, timeout=timeout) as resp2:
                    return getattr(resp2, "status", 200), resp2.read()
        raise


def _check_discord_guild_role(target: str, mode: str) -> Tuple[bool, str]:
    """
    Shared helper to verify whether a Discord user holds Administrator or Moderator
    privileges on the configured Discord guild.
    mode: 'admin' or 'mod'.
    """
    import json
    import urllib.error
    from wd_identity.config import settings

    if not settings.DISCORD_BOT_TOKEN or not settings.DISCORD_GUILD_ID:
        return False, "Discord Bot-Token oder Guild-ID nicht konfiguriert."

    clean_target = target.strip().replace("<@", "").replace(">", "").replace("!", "").lower()
    if not clean_target:
        return False, "Leere Discord-Zielangabe."

    headers = {
        "Authorization": f"Bot {settings.DISCORD_BOT_TOKEN.strip()}",
        "User-Agent": "DiscordBot-WD7 (https://tigimax.mooo.com)",
        "Content-Type": "application/json",
    }
    guild_id = settings.DISCORD_GUILD_ID

    try:
        # 1. Guild owner check
        g_url = f"https://discord.com/api/v10/guilds/{guild_id}"
        _, g_body = _discord_rest_request(g_url, headers=headers, timeout=10)
        guild_data = json.loads(g_body.decode("utf-8"))
        owner_id = str(guild_data.get("owner_id"))

        # 2. Guild roles check
        r_url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
        _, r_body = _discord_rest_request(r_url, headers=headers, timeout=10)
        roles_data = json.loads(r_body.decode("utf-8"))

        allowed_role_ids = set()
        admin_cfg_id = str(getattr(settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371")).strip()
        mod_cfg_id = str(getattr(settings, "DISCORD_MOD_ROLE_ID", "")).strip()

        if mode == "admin":
            if admin_cfg_id:
                allowed_role_ids.add(admin_cfg_id)
        else:
            if mod_cfg_id:
                allowed_role_ids.add(mod_cfg_id)
            if admin_cfg_id:
                allowed_role_ids.add(admin_cfg_id)

        role_names = {}
        for r in roles_data:
            rid = str(r.get("id"))
            rname = r.get("name", rid)
            role_names[rid] = rname
            perms = int(r.get("permissions", 0))
            if perms & 0x8 == 0x8:
                allowed_role_ids.add(rid)
            if mode == "mod":
                rname_lower = rname.lower()
                if "mod" in rname_lower or "moderator" in rname_lower:
                    allowed_role_ids.add(rid)

        # 3. Find member (try direct member lookup first when target is numeric ID)
        found_member = None
        if clean_target.isdigit():
            member_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{clean_target}"
            try:
                _, m_body = _discord_rest_request(member_url, headers=headers, timeout=10)
                found_member = json.loads(m_body.decode("utf-8"))
            except urllib.error.HTTPError as he:
                if he.code != 404:
                    raise

        if found_member is None and not clean_target.isdigit():
            m_url = f"https://discord.com/api/v10/guilds/{guild_id}/members?limit=1000"
            _, m_body = _discord_rest_request(m_url, headers=headers, timeout=10)
            members_data = json.loads(m_body.decode("utf-8"))
            for m in members_data:
                u = m.get("user", {})
                uname = (u.get("username") or "").lower()
                if clean_target == uname:
                    found_member = m
                    break

        if not found_member:
            return False, f"Discord-Nutzer '{target}' wurde auf dem Weihnachtsdorf-Server nicht gefunden."

        m_uid = str(found_member.get("user", {}).get("id"))
        if m_uid == owner_id:
            return True, "Server-Eigentümer (Guild Owner)"

        m_roles = [str(r) for r in found_member.get("roles", [])]
        role_label = "Administrator" if mode == "admin" else "Moderator"
        for rid in m_roles:
            if rid in allowed_role_ids:
                r_name = role_names.get(rid, rid)
                return True, f"Discord {role_label}-Rolle ('{r_name}')"

        return False, f"Nutzer '{target}' besitzt auf dem Discord-Server keine {role_label}-Rolle."

    except Exception as e:
        return False, f"Fehler bei der Discord-Berechtigungsprüfung: {e}"


def check_is_discord_admin(target: str) -> Tuple[bool, str]:
    """
    Checks whether a given Discord user (by ID, username, global name, or mention)
    is currently an Administrator or Guild Owner on the configured Discord guild.
    Returns (is_admin: bool, reason_or_error: str).
    """
    return _check_discord_guild_role(target, mode="admin")


def check_is_discord_mod(target: str) -> Tuple[bool, str]:
    """
    Checks whether a given Discord user (by ID, username, global name, or mention)
    currently has a Moderator role (or Administrator/Owner) on the configured Discord guild.
    Returns (is_mod: bool, reason_or_error: str).
    """
    return _check_discord_guild_role(target, mode="mod")


def resolve_discord_user(discord_name_or_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Resolves any Discord input (numeric ID, mention '<@123>', '<@!123>', or '@username', 'username')
    to a tuple of (discord_id, username). Returns (None, None) if not resolvable.
    """
    from wd_identity.config import settings
    import json
    import logging
    import urllib.parse

    logger = logging.getLogger(__name__)

    if not discord_name_or_id:
        return None, None

    raw = discord_name_or_id.strip()
    clean = raw.replace("<@", "").replace("<@!", "").replace(">", "").replace("!", "").strip()
    if clean.startswith("@"):
        clean = clean[1:].strip()

    if not clean:
        return None, None

    if not settings.DISCORD_BOT_TOKEN:
        return (clean if clean.isdigit() else None), clean

    headers = {
        "Authorization": f"Bot {settings.DISCORD_BOT_TOKEN.strip()}",
        "User-Agent": "DiscordBot-WD7 (https://tigimax.mooo.com)",
        "Content-Type": "application/json",
    }
    guild_id = settings.DISCORD_GUILD_ID or "909437200125538364"

    if clean.isdigit():
        # Fetch username for ID
        url = f"https://discord.com/api/v10/users/{clean}"
        try:
            _, body = _discord_rest_request(url, headers=headers, timeout=5)
            u = json.loads(body.decode())
            return clean, u.get("username", clean)
        except Exception:
            return clean, clean
    else:
        # Search member by name in guild
        q_enc = urllib.parse.quote(clean)
        url = f"https://discord.com/api/v10/guilds/{guild_id}/members/search?query={q_enc}&limit=10"
        try:
            _, body = _discord_rest_request(url, headers=headers, timeout=5)
            members = json.loads(body.decode())
            target_lower = clean.lower()
            for m in members:
                u = m.get("user", {})
                uname = (u.get("username") or "").lower()
                gname = (u.get("global_name") or "").lower()
                nick = (m.get("nick") or "").lower()
                if target_lower in (uname, gname, nick):
                    return str(u.get("id")), u.get("username")
        except Exception as e:
            logger.warning(f"Failed to search member '{clean}' in guild {guild_id}: {e}")

    return None, None


def send_dm_rest(discord_name_or_id: str, message: str) -> bool:
    """
    Sends a direct message using Discord REST API.
    Operates synchronously and independently of the gateway connection,
    ensuring 100% reliable DM delivery from CLI, webhooks, and scripts.
    """
    from wd_identity.config import settings
    import json
    import logging

    logger = logging.getLogger(__name__)

    if not settings.DISCORD_BOT_TOKEN:
        logger.info(
            f"[Discord DM Dispatch (Mock/No Token)] To: {discord_name_or_id} | Message:\n{message}"
        )
        return False

    user_id, _ = resolve_discord_user(discord_name_or_id)
    if not user_id:
        logger.error(f"Could not resolve Discord user ID for '{discord_name_or_id}'")
        return False

    token = settings.DISCORD_BOT_TOKEN.strip()
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot-WD7 (https://tigimax.mooo.com)",
        "Content-Type": "application/json",
    }

    channel_id = None
    try:
        dm_channel_url = "https://discord.com/api/v10/users/@me/channels"
        data = json.dumps({"recipient_id": str(user_id)}).encode("utf-8")
        _, body = _discord_rest_request(dm_channel_url, headers=headers, data=data, method="POST", timeout=10)
        channel_info = json.loads(body.decode())
        channel_id = channel_info.get("id")
    except Exception as e:
        logger.error(f"Failed to create DM channel for user {user_id}: {e}")
        return False

    if not channel_id:
        return False

    try:
        msg_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        data = json.dumps({"content": message}).encode("utf-8")
        _discord_rest_request(msg_url, headers=headers, data=data, method="POST", timeout=10)
        logger.info(f"Successfully dispatched Discord DM to {discord_name_or_id} (ID: {user_id})")
        return True
    except Exception as e:
        logger.error(f"Failed to post DM message to channel {channel_id}: {e}")
        return False


def send_dm(discord_name_or_id: str, message: str) -> bool:
    """Synchronous DM dispatcher."""
    return send_dm_rest(discord_name_or_id, message)
