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
        clean_uuid = raw_uuid.replace("-", "") if raw_uuid and raw_uuid != "0" else "steve"
        avatar_url = f"https://mc-heads.net/avatar/{clean_uuid}/64"
        raw_msg = str(event.get("message") or "")

        if ev_type == "webchat":
            content = f"🌐 {escape_markdown(raw_msg)}"[:1900]
        elif ev_type == "chat":
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
    payload = json.dumps([
        {
            "text": "[D] ",
            "color": "blue",
            "bold": True,
            "hoverEvent": {"action": "show_text", "contents": "Discord"}
        },
        {"text": f"<{ign}> ", "color": "gold"},
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
    return False


def console_filter(lines: List[str]) -> List[str]:
    """Filters out bot-generated console lines (playerlist queries, TPS checks, Weihnachtsdorf tellraw echoes)."""
    return [line for line in lines if not _is_bot_echo(line)]


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
