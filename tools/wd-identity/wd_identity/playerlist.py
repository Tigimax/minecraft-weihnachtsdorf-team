"""
Player list generator and real-time synchronizer for Weihnachtsdorf 7.
Maintains a beautifully designed, pinned participant embed in #spielerliste.
Automatically updates whenever users complete onboarding, are promoted, demoted, or deleted.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

import discord

from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient

logger = logging.getLogger(__name__)

STATE_FILE = Path("/var/lib/wd-identity/playerlist_state.json")

_member_cache: Dict[str, str] = {}
_member_cache_time: float = 0.0


def _get_headers() -> Dict[str, str]:
    token = settings.DISCORD_BOT_TOKEN.strip()
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot-WD7 (https://tigimax.mooo.com)",
        "Content-Type": "application/json",
    }


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read playerlist state: {e}")
    return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save playerlist state: {e}")


def get_guild_member_map() -> Dict[str, str]:
    global _member_cache, _member_cache_time
    now = time.time()
    if _member_cache and (now - _member_cache_time) < 300:
        return _member_cache

    if not settings.DISCORD_BOT_TOKEN or not settings.DISCORD_GUILD_ID:
        return {}

    mapping: Dict[str, str] = {}
    try:
        url = f"https://discord.com/api/v10/guilds/{settings.DISCORD_GUILD_ID}/members?limit=1000"
        req = urllib.request.Request(url, headers=_get_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            members = json.loads(resp.read().decode("utf-8"))
            for m in members:
                u = m.get("user", {})
                uid = str(u.get("id"))
                uname = (u.get("username") or "").lower()
                gname = (u.get("global_name") or "").lower()
                nick = (m.get("nick") or "").lower()
                if uname:
                    mapping[uname] = uid
                if gname:
                    mapping[gname] = uid
                if nick:
                    mapping[nick] = uid
        _member_cache = mapping
        _member_cache_time = now
    except Exception as e:
        logger.warning(f"Failed to fetch guild members for playerlist mapping: {e}")

    return mapping


def _format_entry(u: Dict[str, Any], role: str, is_live: bool, member_map: Dict[str, str]) -> str:
    ign = u.get("minecraftName")
    uid = u.get("uid") or ""
    d_name = u.get("discordName") or uid
    d_id = u.get("discordId") or member_map.get(d_name.lower())

    tag = f"<@{d_id}>" if d_id else f"`@{d_name}`"

    if role == "admin":
        icon = "🛡️"
    elif role == "mod":
        icon = "⚔️"
    else:
        icon = "⛄"

    if ign:
        line = f"• {icon} **{ign}** • {tag}"
    else:
        line = f"• {icon} **{uid}** • {tag} *(Account in Einrichtung)*"

    if role == "player":
        if not is_live:
            line += " *(Pre-Registered)*"
        else:
            line += " *(Freigeschaltet)*"

    return line


def build_playerlist_data(member_map: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], str]:
    if member_map is None:
        member_map = get_guild_member_map()

    ldap = LDAPClient()
    admins, mods = ldap.get_admins_and_mods()
    players = ldap.get_players()

    mc = MinecraftSyncClient()
    is_live = mc.is_project_launched()

    # Sort users for consistent ordering
    admins_sorted = sorted(admins, key=lambda x: (x.get("minecraftName") or x.get("uid", "")).lower())
    mods_sorted = sorted(mods, key=lambda x: (x.get("minecraftName") or x.get("uid", "")).lower())
    players_sorted = sorted(players, key=lambda x: (x.get("minecraftName") or x.get("uid", "")).lower())

    # Build unique hash
    raw_hash_data = {
        "is_live": is_live,
        "admins": [(a.get("uid"), a.get("minecraftName"), a.get("discordName")) for a in admins_sorted],
        "mods": [(m.get("uid"), m.get("minecraftName"), m.get("discordName")) for m in mods_sorted],
        "players": [(p.get("uid"), p.get("minecraftName"), p.get("discordName")) for p in players_sorted],
    }
    content_hash = hashlib.sha256(json.dumps(raw_hash_data, sort_keys=True).encode()).hexdigest()

    admin_lines = [_format_entry(a, "admin", is_live, member_map) for a in admins_sorted]
    mod_lines = [_format_entry(m, "mod", is_live, member_map) for m in mods_sorted]
    player_lines = [_format_entry(p, "player", is_live, member_map) for p in players_sorted]

    total_count = len(admins) + len(mods) + len(players)
    status_text = "🟢 Live auf dem Server • Whitelist aktiv" if is_live else "🟡 Pre-Launch (Vorbereitung) • Whitelist gestaged"

    fields: List[Dict[str, Any]] = []

    # 1. Staff / Admins
    if admin_lines:
        fields.append({
            "name": f"👑 Projektleitung & Administration ({len(admins_sorted)})",
            "value": "\n".join(admin_lines),
            "inline": False,
        })

    # 2. Moderation (if any)
    if mod_lines:
        fields.append({
            "name": f"⚔️ Moderation ({len(mods_sorted)})",
            "value": "\n".join(mod_lines),
            "inline": False,
        })

    # 3. Regular Players (chunk into 20 players per field if necessary)
    if not player_lines:
        fields.append({
            "name": "🎮 Angemeldete Spieler (0)",
            "value": "*Noch keine regulären Spieler angemeldet.*",
            "inline": False,
        })
    else:
        chunk_size = 20
        for i in range(0, len(player_lines), chunk_size):
            chunk = player_lines[i:i + chunk_size]
            title = f"🎮 Angemeldete Spieler ({len(players_sorted)})" if len(player_lines) <= chunk_size else f"🎮 Angemeldete Spieler ({i + 1}–{min(i + chunk_size, len(player_lines))} von {len(players_sorted)})"
            fields.append({
                "name": title,
                "value": "\n".join(chunk),
                "inline": False,
            })

    # 4. Info & Bewerbung
    anmeldung_id = getattr(settings, "DISCORD_CHANNEL_ANMELDUNG_ID", "")
    anmeldung_mention = f"<#{anmeldung_id}>" if anmeldung_id else "#anmeldung"
    fields.append({
        "name": "📝 Du möchtest auch mitspielen?",
        "value": f"Bewerbungen können direkt im Kanal {anmeldung_mention} per Button oder mit dem Befehl `/anmelden` eingereicht werden.",
        "inline": False,
    })

    embed_dict = {
        "title": "🎄 Weihnachtsdorf 7 • Offizielle Teilnehmerliste ❄️",
        "description": (
            "Herzlich willkommen im weihnachtlichen Community-Projekt **Weihnachtsdorf 7**! 🎅✨\n"
            "Hier findest du die aktuelle Übersicht aller freigeschalteten Teammitglieder und Spieler.\n\n"
            f"📡 **Server-Status:** `{status_text}`\n"
            f"🌐 **Server-Adresse:** `tigimax.mooo.com` *(Purpur 26.3)*\n"
            f"👥 **Registriert:** **{total_count}** Teilnehmer (`{len(admins_sorted) + len(mods_sorted)}` Team • `{len(players_sorted)}` Spieler)\n"
        ),
        "color": 0x1f8b4c,
        "thumbnail": {
            "url": "https://cdn.discordapp.com/icons/909437200125538364/8f9dfac93f8187fb9f98ed058aebd84d.png",
        },
        "fields": fields,
        "footer": {
            "text": "Weihnachtsdorf 7 Identity System • Automatisch aktualisiert",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return embed_dict, content_hash


def build_discord_embed(member_map: Optional[Dict[str, str]] = None) -> Tuple[discord.Embed, str]:
    embed_dict, content_hash = build_playerlist_data(member_map)
    embed = discord.Embed.from_dict(embed_dict)
    return embed, content_hash


def sync_playerlist_rest(force: bool = False) -> bool:
    """
    Synchronously updates the pinned playerlist message in Discord using the REST API.
    Can be called from CLI, Webhook handlers, or cron jobs.
    """
    if not settings.DISCORD_BOT_TOKEN or not settings.DISCORD_GUILD_ID:
        logger.warning("Discord bot token or guild ID not configured, skipping playerlist sync.")
        return False

    state = load_state()
    channel_id = getattr(settings, "DISCORD_CHANNEL_SPIELERLISTE_ID", "") or state.get("channel_id")
    if not channel_id:
        logger.error("No DISCORD_CHANNEL_SPIELERLISTE_ID available.")
        return False

    embed_dict, content_hash = build_playerlist_data()

    # Skip if nothing changed and not forced
    if not force and state.get("last_hash") == content_hash and state.get("message_id"):
        return True

    message_id = state.get("message_id")
    success = False

    # Try editing existing message
    if message_id:
        edit_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
        payload = json.dumps({"embeds": [embed_dict]}).encode("utf-8")
        req = urllib.request.Request(edit_url, data=payload, headers=_get_headers(), method="PATCH")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Successfully edited playerlist message {message_id}")
                    success = True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.warning(f"Playerlist message {message_id} was deleted in Discord. Recreating...")
                message_id = None
            else:
                logger.error(f"Failed to edit playerlist message: {e}")
                return False
        except Exception as e:
            logger.error(f"Error editing playerlist message: {e}")
            return False

    # If message does not exist or was deleted, post and pin a new one
    if not message_id:
        post_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        payload = json.dumps({"embeds": [embed_dict]}).encode("utf-8")
        req = urllib.request.Request(post_url, data=payload, headers=_get_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                msg_data = json.loads(resp.read().decode("utf-8"))
                message_id = msg_data.get("id")
                logger.info(f"Created new playerlist message {message_id}")
                success = True
        except Exception as e:
            logger.error(f"Failed to post playerlist message: {e}")
            return False

        if message_id:
            # Pin the message
            pin_url = f"https://discord.com/api/v10/channels/{channel_id}/pins/{message_id}"
            try:
                pin_req = urllib.request.Request(pin_url, headers=_get_headers(), method="PUT")
                with urllib.request.urlopen(pin_req, timeout=10):
                    logger.info(f"Pinned playerlist message {message_id}")
            except Exception as e:
                logger.warning(f"Failed to pin playerlist message: {e}")

            # Delete system pin notification message
            try:
                get_url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=10"
                get_req = urllib.request.Request(get_url, headers=_get_headers(), method="GET")
                with urllib.request.urlopen(get_req, timeout=10) as r:
                    recent = json.loads(r.read().decode("utf-8"))
                    for rm in recent:
                        if rm.get("type") == 6:  # PINS_ADD
                            del_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{rm['id']}"
                            del_req = urllib.request.Request(del_url, headers=_get_headers(), method="DELETE")
                            with urllib.request.urlopen(del_req, timeout=5):
                                pass
            except Exception:
                pass

    if success and message_id:
        state["channel_id"] = str(channel_id)
        state["message_id"] = str(message_id)
        state["last_hash"] = content_hash
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return True

    return False


async def sync_playerlist_bot(bot: discord.Client, force: bool = False) -> bool:
    """
    Asynchronously updates the playerlist message using an active discord.py Bot instance.
    """
    guild_id = getattr(settings, "DISCORD_GUILD_ID", None)
    if not guild_id:
        return False

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return False

    channel_id = getattr(settings, "DISCORD_CHANNEL_SPIELERLISTE_ID", "")
    ch = guild.get_channel(int(channel_id)) if channel_id and channel_id.isdigit() else None

    # Try finding channel by name if not found by ID
    if not ch:
        for c in guild.text_channels:
            if any(k in c.name.lower() for k in ("spielerliste", "teilnehmer", "teilnehmerliste")):
                ch = c
                break

    if not ch:
        logger.warning("Could not find #spielerliste channel in bot cache.")
        return False

    # Build member map from guild cache
    member_map: Dict[str, str] = {}
    for m in guild.members:
        uid = str(m.id)
        if m.name:
            member_map[m.name.lower()] = uid
        if m.global_name:
            member_map[m.global_name.lower()] = uid
        if m.nick:
            member_map[m.nick.lower()] = uid

    embed, content_hash = build_discord_embed(member_map)
    state = load_state()

    if not force and state.get("last_hash") == content_hash and state.get("message_id"):
        return True

    message_id = state.get("message_id")
    target_msg: Optional[discord.Message] = None

    if message_id:
        try:
            target_msg = await ch.fetch_message(int(message_id))
        except discord.NotFound:
            target_msg = None
        except Exception as e:
            logger.warning(f"Error fetching existing message {message_id}: {e}")

    # If no message found by state ID, look in recent pins or messages from bot
    if not target_msg:
        try:
            pins = await ch.pins()
            for p in pins:
                if p.author.id == bot.user.id and p.embeds and "Teilnehmerliste" in (p.embeds[0].title or ""):
                    target_msg = p
                    break
        except Exception:
            pass

    if target_msg:
        try:
            await target_msg.edit(embed=embed)
            state["message_id"] = str(target_msg.id)
            state["channel_id"] = str(ch.id)
            state["last_hash"] = content_hash
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            return True
        except Exception as e:
            logger.error(f"Failed to edit playerlist message via bot: {e}")

    # If no message exists, send and pin
    try:
        new_msg = await ch.send(embed=embed)
        await new_msg.pin()
        state["message_id"] = str(new_msg.id)
        state["channel_id"] = str(ch.id)
        state["last_hash"] = content_hash
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        # Cleanup pin notification
        async for m in ch.history(limit=5):
            if m.type == discord.MessageType.pins_add:
                try:
                    await m.delete()
                except Exception:
                    pass
        return True
    except Exception as e:
        logger.error(f"Failed to send/pin playerlist message via bot: {e}")
        return False
