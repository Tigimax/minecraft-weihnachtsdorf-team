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


def check_is_discord_admin(target: str) -> Tuple[bool, str]:
    """
    Checks whether a given Discord user (by ID, username, global name, or mention)
    is currently an Administrator or Guild Owner on the configured Discord guild.
    Returns (is_admin: bool, reason_or_error: str).
    """
    import json
    import urllib.request
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
        req = urllib.request.Request(g_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            guild_data = json.loads(resp.read().decode("utf-8"))
            owner_id = str(guild_data.get("owner_id"))

        # 2. Guild roles check (find all roles with ADMINISTRATOR 0x8 permission)
        r_url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
        req = urllib.request.Request(r_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            roles_data = json.loads(resp.read().decode("utf-8"))

        admin_role_ids = set()
        admin_cfg_id = str(getattr(settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371"))
        if admin_cfg_id:
            admin_role_ids.add(admin_cfg_id)

        role_names = {}
        for r in roles_data:
            rid = str(r.get("id"))
            role_names[rid] = r.get("name", rid)
            perms = int(r.get("permissions", 0))
            if perms & 0x8 == 0x8:
                admin_role_ids.add(rid)

        # 3. Find member
        m_url = f"https://discord.com/api/v10/guilds/{guild_id}/members?limit=1000"
        req = urllib.request.Request(m_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            members_data = json.loads(resp.read().decode("utf-8"))

        found_member = None
        for m in members_data:
            u = m.get("user", {})
            uid = str(u.get("id"))
            uname = (u.get("username") or "").lower()
            gname = (u.get("global_name") or "").lower()
            nick = (m.get("nick") or "").lower()

            if clean_target.isdigit() and uid == clean_target:
                found_member = m
                break
            if clean_target in (uname, gname, nick):
                found_member = m
                break

        if not found_member:
            return False, f"Discord-Nutzer '{target}' wurde auf dem Weihnachtsdorf-Server nicht gefunden."

        m_uid = str(found_member.get("user", {}).get("id"))
        if m_uid == owner_id:
            return True, "Server-Eigentümer (Guild Owner)"

        m_roles = [str(r) for r in found_member.get("roles", [])]
        for rid in m_roles:
            if rid in admin_role_ids:
                r_name = role_names.get(rid, rid)
                return True, f"Discord Administrator-Rolle ('{r_name}')"

        return False, f"Nutzer '{target}' besitzt auf dem Discord-Server keine Administrator-Rolle."

    except Exception as e:
        return False, f"Fehler bei der Discord-Berechtigungsprüfung: {e}"


def resolve_discord_user(discord_name_or_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Resolves any Discord input (numeric ID, mention '<@123>', '<@!123>', or '@username', 'username')
    to a tuple of (discord_id, username). Returns (None, None) if not resolvable.
    """
    from wd_identity.config import settings
    import json
    import logging
    import urllib.parse
    import urllib.request

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
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                u = json.loads(r.read().decode())
                return clean, u.get("username", clean)
        except Exception:
            return clean, clean
    else:
        # Search member by name in guild
        q_enc = urllib.parse.quote(clean)
        url = f"https://discord.com/api/v10/guilds/{guild_id}/members/search?query={q_enc}&limit=10"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                members = json.loads(r.read().decode())
                target_lower = clean.lower()
                for m in members:
                    u = m.get("user", {})
                    uname = (u.get("username") or "").lower()
                    gname = (u.get("global_name") or "").lower()
                    nick = (m.get("nick") or "").lower()
                    if target_lower in (uname, gname, nick):
                        return str(u.get("id")), u.get("username")
                if members:
                    u = members[0].get("user", {})
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
    import urllib.request

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
        req = urllib.request.Request(dm_channel_url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            channel_info = json.loads(resp.read().decode())
            channel_id = channel_info.get("id")
    except Exception as e:
        logger.error(f"Failed to create DM channel for user {user_id}: {e}")
        return False

    if not channel_id:
        return False

    try:
        msg_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(msg_url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Successfully dispatched Discord DM to {discord_name_or_id} (ID: {user_id})")
            return True
    except Exception as e:
        logger.error(f"Failed to post DM message to channel {channel_id}: {e}")
        return False


def send_dm(discord_name_or_id: str, message: str) -> bool:
    """Synchronous DM dispatcher."""
    return send_dm_rest(discord_name_or_id, message)
