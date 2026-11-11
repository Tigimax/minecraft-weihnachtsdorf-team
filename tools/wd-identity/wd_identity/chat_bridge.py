"""
Chat and Console Bridge between Minecraft, BlueMap, and Discord for Weihnachtsdorf 7.
Enables:
- Bi-directional chat forwarding between Discord and Minecraft/BlueMap
- Authenticated player identity resolution via LDAP
- In-game tellraw broadcasting with custom hover and click events
- Live console log streaming and strict administrative command execution
"""

import asyncio
from collections import deque
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from discord.utils import escape_markdown

from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient

logger = logging.getLogger(__name__)

# Bounded queue for Minecraft -> Discord events (created per active event loop)
_discord_queue: Optional[asyncio.Queue] = None
_queue_loop: Optional[asyncio.AbstractEventLoop] = None
_worker_task: Optional[asyncio.Task] = None
_webhook_cache: Dict[str, str] = {}


def _get_queue() -> asyncio.Queue:
    """Returns or initializes the bounded queue for the current running event loop."""
    global _discord_queue, _queue_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _discord_queue is None or (loop is not None and _queue_loop is not loop):
        _discord_queue = asyncio.Queue(maxsize=200)
        _queue_loop = loop
    return _discord_queue

# Player cache: discord_id -> ((ign, uuid) or None, expire_timestamp)
_player_cache: Dict[str, Tuple[Optional[Tuple[str, str]], float]] = {}

# Minecraft player lookup cache: ign.lower() -> (info_dict, expire_timestamp)
_minecraft_player_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}

# Discord avatar cache: discord_id -> (avatar_url, expire_timestamp)
_avatar_cache: Dict[str, Tuple[str, float]] = {}

ROLE_COLORS = {
    "admin": "red",
    "mod": "blue",
    "player": "white",
}

# Rate limits: user_id -> deque of timestamps
_user_rate_limits: Dict[str, deque] = {}


def active_chat_channel_id() -> str:
    """
    Returns the currently active Discord chat channel ID.
    If the project is launched, returns DISCORD_CHANNEL_CHAT_LIVE_ID;
    otherwise returns DISCORD_CHANNEL_CHAT_PRELAUNCH_ID.
    """
    try:
        mc = MinecraftSyncClient()
        launched = mc.is_project_launched()
    except Exception as e:
        logger.warning(f"Failed to check project launched state: {e}")
        launched = False

    if launched:
        return str(getattr(settings, "DISCORD_CHANNEL_CHAT_LIVE_ID", "913851557102112799") or "913851557102112799")
    return str(getattr(settings, "DISCORD_CHANNEL_CHAT_PRELAUNCH_ID", "1570031673809043476") or "1570031673809043476")


def sanitize_webhook_username(name: str) -> str:
    """Sanitizes username for Discord Webhooks (replaces forbidden 'discord' / 'clyde', max 80 chars)."""
    name = (name or "Spieler").strip()[:80]
    clean = re.sub(r"(?i)discord", "d!scord", name)
    clean = re.sub(r"(?i)clyde", "clyd_e", clean)
    return clean or "Spieler"


async def _get_channel_webhook_url(client: httpx.AsyncClient, channel_id: str) -> Optional[str]:
    """Retrieves or creates a dedicated 'wd-chat-bridge' webhook for the given channel."""
    if channel_id in _webhook_cache:
        return _webhook_cache[channel_id]

    token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not token:
        logger.warning("DISCORD_BOT_TOKEN is not configured; cannot fetch or create webhook.")
        return None

    headers = {"Authorization": f"Bot {token}"}
    try:
        r = await client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/webhooks",
            headers=headers,
        )
        if r.status_code == 200:
            webhooks = r.json()
            for wh in webhooks:
                if wh.get("name") == "wd-chat-bridge":
                    url = wh.get("url") or (
                        f"https://discord.com/api/webhooks/{wh['id']}/{wh['token']}"
                        if wh.get("id") and wh.get("token")
                        else None
                    )
                    if url:
                        _webhook_cache[channel_id] = url
                        return url

        # Webhook does not exist yet; create it
        cr = await client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/webhooks",
            headers=headers,
            json={"name": "wd-chat-bridge"},
        )
        if cr.status_code in (200, 201):
            wh = cr.json()
            url = wh.get("url") or (
                f"https://discord.com/api/webhooks/{wh['id']}/{wh['token']}"
                if wh.get("id") and wh.get("token")
                else None
            )
            if url:
                _webhook_cache[channel_id] = url
                return url
        logger.warning(f"Could not create webhook for channel {channel_id}: status={cr.status_code}")
    except Exception as e:
        logger.warning(f"Failed to resolve Discord webhook for channel {channel_id}: {e}")
    return None


def resolve_player_by_minecraft(ign: str, uuid: str = "") -> Dict[str, Any]:
    """
    Resolves Minecraft player IGN/UUID to discord_id and role ('admin', 'mod', 'player') via LDAP.
    Results are cached with a 60-second TTL.
    Returns dict: {"discord_id": str, "role": str, "ign": str, "uuid": str}
    """
    key = (ign or "").strip().lower()
    now = time.time()
    if key and key in _minecraft_player_cache:
        val, exp = _minecraft_player_cache[key]
        if now < exp:
            return val

    res = _lookup_player_by_minecraft(ign, uuid)
    if key:
        _minecraft_player_cache[key] = (res, now + 60.0)
    return res


def _lookup_player_by_minecraft(ign: str, uuid: str = "") -> Dict[str, Any]:
    default_res = {"discord_id": "", "role": "player", "ign": ign, "uuid": uuid}
    if not ign or not str(ign).strip():
        return default_res

    clean_ign = ign.strip()
    try:
        ldap = LDAPClient()
        user = None

        # 1. Search by minecraft IGN
        try:
            user = ldap.search_by_minecraft(clean_ign)
        except Exception as e:
            logger.debug(f"search_by_minecraft failed for {clean_ign}: {e}")

        # 2. Fallbacks if user not found (e.g. tigimax vs tigimax_player or by uid)
        lower_ign = clean_ign.lower()
        if not user:
            user = ldap.get_user(f"{lower_ign}_player")
        if not user:
            user = ldap.get_user(lower_ign)

        if not user:
            return default_res

        discord_id = str(user.get("discordId") or "").strip()
        user_uuid = str(user.get("minecraftUuid") or uuid or "")
        user_dn = (user.get("dn") or "").strip().lower()
        user_uid = (user.get("uid") or "").strip().lower()

        # Determine role: admin, mod, or player
        admin_group = getattr(settings, "LDAP_ADMIN_GROUP", "cn=wd_admins,ou=Groups,dc=ldap-01,dc=net")
        mod_group = getattr(settings, "LDAP_MOD_GROUP", "cn=wd_mods,ou=Groups,dc=ldap-01,dc=net")
        player_group = getattr(settings, "LDAP_PLAYER_GROUP", "cn=wd_players,ou=Groups,dc=ldap-01,dc=net")

        role = "player"
        for group_dn, group_role in [
            (admin_group, "admin"),
            (mod_group, "mod"),
            (player_group, "player"),
        ]:
            if not group_dn:
                continue
            try:
                members = ldap.get_group_members(group_dn)
            except Exception as e:
                logger.warning(f"Failed to get members for {group_dn}: {e}")
                continue
            found = False
            for m in members:
                clean_m = str(m).strip().lower()
                if clean_m and (clean_m == user_dn or clean_m == user_uid):
                    role = group_role
                    found = True
                    break
            if found:
                break

        return {
            "discord_id": discord_id,
            "role": role,
            "ign": user.get("minecraftName") or ign,
            "uuid": user_uuid,
        }
    except Exception as e:
        logger.warning(f"Error resolving player by minecraft for '{ign}': {e}")
        return default_res


async def get_discord_avatar_url(client: httpx.AsyncClient, discord_id: str) -> str:
    """
    Fetches the Discord avatar URL for a user (Guild member avatar, User avatar, or default embed avatar).
    Cached with a 60s TTL.
    """
    if not discord_id or not str(discord_id).strip():
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    did = str(discord_id).strip()
    now = time.time()
    if did in _avatar_cache:
        val, exp = _avatar_cache[did]
        if now < exp:
            return val

    # Calculate default embed avatar
    try:
        did_int = int(did)
        default_avatar = f"https://cdn.discordapp.com/embed/avatars/{(did_int >> 22) % 6}.png"
    except (ValueError, TypeError):
        default_avatar = "https://cdn.discordapp.com/embed/avatars/0.png"

    token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    guild_id = getattr(settings, "DISCORD_GUILD_ID", "")

    if not token:
        _avatar_cache[did] = (default_avatar, now + 60.0)
        return default_avatar

    headers = {"Authorization": f"Bot {token}"}

    try:
        # Try guild member avatar first
        if guild_id:
            res = await client.get(
                f"https://discord.com/api/v10/guilds/{guild_id}/members/{did}",
                headers=headers,
            )
            if res.status_code == 200:
                member_data = res.json()
                guild_av = member_data.get("avatar")
                user_av = member_data.get("user", {}).get("avatar")
                if guild_av:
                    url = f"https://cdn.discordapp.com/guilds/{guild_id}/users/{did}/avatars/{guild_av}.png?size=128"
                    _avatar_cache[did] = (url, now + 60.0)
                    return url
                elif user_av:
                    url = f"https://cdn.discordapp.com/avatars/{did}/{user_av}.png?size=128"
                    _avatar_cache[did] = (url, now + 60.0)
                    return url
                else:
                    _avatar_cache[did] = (default_avatar, now + 60.0)
                    return default_avatar

        # Fallback to direct user endpoint
        u_res = await client.get(
            f"https://discord.com/api/v10/users/{did}",
            headers=headers,
        )
        if u_res.status_code == 200:
            user_data = u_res.json()
            user_av = user_data.get("avatar")
            if user_av:
                url = f"https://cdn.discordapp.com/avatars/{did}/{user_av}.png?size=128"
                _avatar_cache[did] = (url, now + 60.0)
                return url

    except Exception as e:
        logger.warning(f"Failed to resolve Discord avatar for {did}: {e}")

    _avatar_cache[did] = (default_avatar, now + 60.0)
    return default_avatar


async def _send_event_to_webhook(client: httpx.AsyncClient, event: dict) -> None:
    """Dispatches a single formatted game event to Discord via channel webhook."""
    try:
        chan_id = str(await asyncio.to_thread(active_chat_channel_id))
        webhook_url = await _get_channel_webhook_url(client, chan_id)
        if not webhook_url:
            logger.warning(f"No webhook available for active channel {chan_id}; dropping event.")
            return

        ev_type = (event.get("type") or "chat").lower()
        ign = event.get("username") or "Spieler"
        raw_uuid = str(event.get("uuid") or "")

        # Resolve player discord_id and avatar
        player_info = await asyncio.to_thread(resolve_player_by_minecraft, ign, raw_uuid)
        discord_id = player_info.get("discord_id", "")
        if discord_id:
            avatar_url = await get_discord_avatar_url(client, discord_id)
        else:
            avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

        raw_msg = str(event.get("message") or "")

        if ev_type in ("webchat", "chat"):
            content = escape_markdown(raw_msg)[:1900]
        elif ev_type == "join":
            content = f"➕ **{escape_markdown(ign)}** hat den Server betreten"
        elif ev_type == "quit":
            content = f"➖ **{escape_markdown(ign)}** hat den Server verlassen"
        elif ev_type == "death":
            content = f"💀 {escape_markdown(raw_msg)}"
        else:
            content = escape_markdown(raw_msg)[:1900]

        if not content:
            return

        payload = {
            "username": sanitize_webhook_username(ign),
            "avatar_url": avatar_url,
            "content": content,
            "allowed_mentions": {"parse": []},
        }

        resp = await client.post(webhook_url, json=payload)
        if resp.status_code == 429:
            try:
                retry_after = min(float(resp.json().get("retry_after", 1)), 5.0)
            except Exception:
                retry_after = 1.0
            await asyncio.sleep(retry_after)
            resp = await client.post(webhook_url, json=payload)

        if resp.status_code in (401, 403, 404):
            _webhook_cache.pop(chan_id, None)
            logger.warning(
                f"Discord webhook for channel {chan_id} invalid or deleted (HTTP {resp.status_code}); cleared from cache."
            )
        elif resp.status_code >= 400:
            logger.warning(f"Discord webhook POST returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"Failed to post event to Discord webhook: {e}")


async def _discord_worker_loop() -> None:
    """Persistent background worker loop processing enqueued Discord messages."""
    q = _get_queue()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                event = await q.get()
                try:
                    await _send_event_to_webhook(client, event)
                except Exception as e:
                    logger.warning(f"Unhandled error in chat bridge worker: {e}")
                finally:
                    q.task_done()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Chat bridge worker terminated with error: {e}")


async def enqueue_to_discord(event: dict) -> None:
    """
    Enqueues a game or webchat event to be posted to Discord.
    Drops oldest event if queue exceeds capacity (maxsize=200).
    Ignores events originating from Discord to prevent feedback loops.
    """
    if not isinstance(event, dict):
        return
    if event.get("source") == "discord":
        return

    global _worker_task
    q = _get_queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_discord_worker_loop())

    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass

    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(event)
        except Exception:
            pass


def clean_discord_text(message: Any) -> str:
    """
    Cleans a Discord message:
    - Strips custom emojis (<:name:id> -> :name:)
    - Converts newlines to spaces
    - Appends [Bild] for attachments / stickers
    - Truncates to 256 characters
    """
    text = getattr(message, "clean_content", "") or ""
    # Convert custom discord emojis <:name:id> and animated <a:name:id> to :name:
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    # Convert newlines to spaces
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.strip()

    has_media = bool(getattr(message, "attachments", None) or getattr(message, "stickers", None))
    if has_media:
        text = f"{text} [Bild]".strip() if text else "[Bild]"

    return text[:256]


def build_tellraw(ign: str, text: str, role: str = "") -> str:
    """Builds the Minecraft tellraw command for broadcasting a Discord chat message."""
    if not role:
        try:
            player_info = resolve_player_by_minecraft(ign)
            role = player_info.get("role", "player")
        except Exception:
            role = "player"

    name_color = ROLE_COLORS.get((role or "").lower(), "white")

    payload = json.dumps([
        {
            "text": "[D] ",
            "color": "blue",
            "bold": True,
            "hoverEvent": {"action": "show_text", "contents": "Discord"}
        },
        {"text": f"<{ign}> ", "color": name_color},
        {"text": text, "color": "white"}
    ], ensure_ascii=False)
    return f"execute in minecraft:overworld run tellraw @a {payload}"


def resolve_active_player(discord_id: str) -> Optional[Tuple[str, str]]:
    """
    Resolves Discord user ID to active (ign, uuid).
    Requires linked account with minecraftName AND membership in LDAP player/mod/admin groups.
    Results (both positive and negative) are cached with a 60s TTL.
    """
    if not discord_id or not str(discord_id).strip():
        return None
    key = str(discord_id).strip()
    now = time.time()
    if key in _player_cache:
        val, exp = _player_cache[key]
        if now < exp:
            return val

    res = _lookup_active_player(key)
    _player_cache[key] = (res, now + 60.0)
    return res


def _lookup_active_player(discord_id: str) -> Optional[Tuple[str, str]]:
    """Internal LDAP lookup for active player verification."""
    try:
        ldap = LDAPClient()
        user = ldap.search_by_discord(discord_id)
        if not user:
            return None

        ign = user.get("minecraftName") or ""
        if not ign:
            return None
        uuid = user.get("minecraftUuid") or ""
        user_dn = (user.get("dn") or "").strip().lower()
        user_uid = (user.get("uid") or "").strip().lower()

        allowed_groups = [
            getattr(settings, "LDAP_PLAYER_GROUP", "cn=wd_players,ou=Groups,dc=ldap-01,dc=net"),
            getattr(settings, "LDAP_MOD_GROUP", "cn=wd_mods,ou=Groups,dc=ldap-01,dc=net"),
            getattr(settings, "LDAP_ADMIN_GROUP", "cn=wd_admins,ou=Groups,dc=ldap-01,dc=net"),
        ]

        is_member = False
        for group_dn in allowed_groups:
            if not group_dn:
                continue
            try:
                members = ldap.get_group_members(group_dn)
            except Exception as e:
                logger.warning(f"Failed to get members for {group_dn}: {e}")
                continue
            for m in members:
                clean_m = str(m).strip().lower()
                if clean_m and (clean_m == user_dn or clean_m == user_uid):
                    is_member = True
                    break
            if is_member:
                break

        if not is_member:
            return None

        return (ign, uuid)
    except Exception as e:
        logger.warning(f"Error looking up active player for {discord_id}: {e}")
        return None


def rate_ok(user_id: Any) -> bool:
    """Rate limit: maximum 5 messages per 10 seconds per user."""
    if not user_id:
        return False
    key = str(user_id)
    now = time.time()
    dq = _user_rate_limits.setdefault(key, deque())
    while dq and dq[0] <= now - 10.0:
        dq.popleft()
    if len(dq) < 5:
        dq.append(now)
        return True
    return False


ANSI_ESCAPES = [
    re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]"),
    re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"),
    re.compile(r"\x1b[=>]"),
    re.compile(r"\x1b?\[(?:K|\?[0-9;]+[hl]=?|[0-9;]{1,10}m)"),
    re.compile(r"[\x00-\x08\x0b-\x1f\x7f]"),
]
JLINE_START = re.compile(r"^(?:>+\.{2,}\s*)+")
JLINE_END = re.compile(r"(?:>+\.{2,}\s*)+$")


def clean_console_line(line: str) -> str:
    """Removes \r, ANSI/VT100/JLine escape sequences, control characters, and JLine prompt artifacts."""
    cleaned = line.replace("\r", "")
    for pat in ANSI_ESCAPES:
        cleaned = pat.sub("", cleaned)
    cleaned = JLINE_START.sub("", cleaned)
    cleaned = JLINE_END.sub("", cleaned)
    return cleaned.rstrip()


def _is_bot_echo(line: str) -> bool:
    """Checks if a log line is an automated query or echo from the bot itself."""
    if "There are " in line and "players online" in line:
        return True
    if "TPS from last" in line:
        return True
    line_lower = line.lower()
    if "tellraw" in line_lower:
        if "weihnachtsdorf" in line_lower or "[d]" in line_lower or "[web]" in line_lower:
            return True
        if "tellraw @a" in line_lower:
            return True
    if "no player was found" in line_lower:
        return True
    return False


def console_filter(lines: List[str]) -> List[str]:
    """Filters out bot-generated console lines, ANSI escape sequences, and empty lines."""
    filtered: List[str] = []
    for line in lines:
        cleaned = clean_console_line(line)
        if not cleaned.strip():
            continue
        if _is_bot_echo(cleaned):
            continue
        filtered.append(cleaned)
    return filtered


def chunk_code_blocks(lines: List[str], limit: int = 1900) -> List[str]:
    """Groups console lines into Discord code blocks (``` ... ```) of at most `limit` characters."""
    if not lines:
        return []
    chunks: List[str] = []
    current_lines: List[str] = []
    max_line_len = max(limit - 8, 1)

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if len(line) > max_line_len:
            line = line[:max_line_len]

        candidate_lines = current_lines + [line]
        candidate_block = "```\n" + "\n".join(candidate_lines) + "\n```"
        if len(candidate_block) <= limit:
            current_lines.append(line)
        else:
            if current_lines:
                chunks.append("```\n" + "\n".join(current_lines) + "\n```")
            current_lines = [line]

    if current_lines:
        chunks.append("```\n" + "\n".join(current_lines) + "\n```")

    return chunks
