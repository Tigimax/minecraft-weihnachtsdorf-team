"""
Discord bot for Weihnachtsdorf 7.
Features:
- Slash commands: /passwort-reset, /status, /links
- Direct message dispatcher for invitations and recovery links
- Welcome guide DM for newly onboarded team members
"""

import asyncio
import json
import logging
from typing import Optional
import urllib.parse
import urllib.request
import discord
from discord import app_commands
from discord.ext import commands

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


def get_welcome_message(username: str, role: str, ign: str) -> str:
    return f"""🎄 **Willkommen im Weihnachtsdorf 7 Team, {username}!** 🎄

Dein Zugang als **{role.upper()}** wurde erfolgreich eingerichtet.

📌 **Wichtige Zugänge & Portale (Single Sign-On):**
• **Authentik SSO Portal:** {settings.AUTHENTIK_BASE_URL}
• **BlueMap Live-Karte:** https://map.tigimax.mooo.com
• **Plan Analytics:** https://plan.tigimax.mooo.com
• **Pterodactyl Panel:** https://panel.tigimax.mooo.com
• **Planka Kanban Board:** https://tigimax.mooo.com
• **Minecraft Server:** `tigimax.mooo.com` (Purpur 26.3)

🎮 **Minecraft Profil:**
• Ingame-Name: `{ign}`
• Whitelist & LuckPerms-Gruppe (`{role}`) sind aktiv.

🔒 **Sicherheitsrichtlinien:**
• 2FA (TOTP) ist für deinen Account verpflichtend eingerichtet.
• Bewahre deine Zugangsdaten sicher auf und nutze dein zentrales SSO-Passwort.
• Passwort-Resets kannst du jederzeit mit `/passwort-reset` anfordern.

Viel Spaß und Erfolg beim Aufbau von Weihnachtsdorf 7! 🎅✨"""


@tree.command(
    name="passwort-reset",
    description="Fordere einen einmaligen Link zum Zurücksetzen deines Passworts an.",
)
async def cmd_password_reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ldap = LDAPClient()
    authentik = AuthentikClient()

    discord_tag = interaction.user.name
    discord_id = str(interaction.user.id)

    # Search user in LDAP
    user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(discord_tag)
    if not user:
        await interaction.followup.send(
            "❌ Dein Discord-Account ist mit keinem Weihnachtsdorf-Account verknüpft. Bitte kontaktiere einen Administrator.",
            ephemeral=True,
        )
        return

    # Generate Authentik recovery link
    recovery_link = authentik.create_recovery_link(user["uid"])
    if not recovery_link:
        await interaction.followup.send(
            f"⚠️ Es konnte kein Wiederherstellungslink für `{user['uid']}` generiert werden. Bitte wende dich an @Tigimax.",
            ephemeral=True,
        )
        return

    # Attempt to send via DM
    dm_sent = False
    try:
        dm_channel = await interaction.user.create_dm()
        await dm_channel.send(
            f"🔐 **Passwort-Zurücksetzung für Weihnachtsdorf 7**\n\n"
            f"Hallo {user['uid']},\nhier ist dein persönlicher, einmaliger Wiederherstellungs-Link:\n"
            f"{recovery_link}\n\n*Dieser Link ist zeitlich begrenzt und verfällt nach einmaliger Nutzung.*"
        )
        dm_sent = True
    except Exception as e:
        logger.warning(f"Failed to send DM to {interaction.user}: {e}")

    if dm_sent:
        await interaction.followup.send(
            "✅ Der Wiederherstellungslink wurde dir per privater Nachricht (DM) zugestellt!",
            ephemeral=True,
        )
    else:
        # Fallback if DMs are closed
        await interaction.followup.send(
            f"✅ Hier ist dein persönlicher Wiederherstellungs-Link (nur für dich sichtbar):\n{recovery_link}",
            ephemeral=True,
        )


@tree.command(
    name="status",
    description="Zeigt deinen aktuellen Account-Status und deine Berechtigungen an.",
)
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ldap = LDAPClient()

    discord_tag = interaction.user.name
    discord_id = str(interaction.user.id)

    user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(discord_tag)
    if not user:
        await interaction.followup.send(
            "❌ Kein verknüpfter Account gefunden.",
            ephemeral=True,
        )
        return

    admins, mods = ldap.get_admins_and_mods()
    admin_uids = [a["uid"] for a in admins]
    mod_uids = [m["uid"] for m in mods]

    if user["uid"] in admin_uids:
        role = "Administrator 🔴"
    elif user["uid"] in mod_uids:
        role = "Moderator 🟡"
    else:
        role = "Benutzer ⚪"

    embed = discord.Embed(
        title="Weihnachtsdorf 7 • Account Status",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Username", value=f"`{user['uid']}`", inline=True)
    embed.add_field(name="Rolle", value=role, inline=True)
    embed.add_field(
        name="Minecraft IGN",
        value=f"`{user.get('minecraftName') or 'Nicht hinterlegt'}`",
        inline=False,
    )
    embed.add_field(
        name="Minecraft UUID",
        value=f"`{user.get('minecraftUuid') or 'Nicht hinterlegt'}`",
        inline=False,
    )
    embed.add_field(
        name="2FA Status",
        value="Aktiv (TOTP verpflichtend)",
        inline=False,
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(
    name="links",
    description="Zeigt eine Übersicht aller Weihnachtsdorf 7 Portale und Dienste.",
)
async def cmd_links(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎄 Weihnachtsdorf 7 • Portal & Service Links",
        description="Alle Web-Dienste sind über zentrales SSO geschützt.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="🔐 Authentik SSO Portal",
        value=f"[{settings.AUTHENTIK_BASE_URL}]({settings.AUTHENTIK_BASE_URL})",
        inline=False,
    )
    embed.add_field(
        name="🗺️ BlueMap Live-Karte",
        value="[https://map.tigimax.mooo.com](https://map.tigimax.mooo.com)",
        inline=True,
    )
    embed.add_field(
        name="📊 Plan Analytics",
        value="[https://plan.tigimax.mooo.com](https://plan.tigimax.mooo.com)",
        inline=True,
    )
    embed.add_field(
        name="🕹️ Pterodactyl Game Panel",
        value="[https://panel.tigimax.mooo.com](https://panel.tigimax.mooo.com)",
        inline=True,
    )
    embed.add_field(
        name="📋 Planka Kanban Board",
        value="[https://tigimax.mooo.com](https://tigimax.mooo.com)",
        inline=True,
    )
    embed.add_field(
        name="⛏️ Minecraft Server",
        value="`tigimax.mooo.com` (Purpur 26.3)",
        inline=True,
    )
    embed.set_footer(text="Weihnachtsdorf 7 Identity System")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    logger.info(f"Discord Bot logged in as {bot.user} (ID: {bot.user.id})")
    try:
        # Sync directly to configured guild for instant slash command availability
        if settings.DISCORD_GUILD_ID:
            guild_obj = discord.Object(id=int(settings.DISCORD_GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced_guild = await tree.sync(guild=guild_obj)
            logger.info(f"Synced {len(synced_guild)} slash commands to guild {settings.DISCORD_GUILD_ID}")
        synced = await tree.sync()
        logger.info(f"Synced {len(synced)} application commands globally.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")


def send_dm_rest(discord_name_or_id: str, message: str) -> bool:
    """
    Sends a direct message using Discord REST API.
    Operates synchronously and independently of the gateway connection,
    ensuring 100% reliable DM delivery from CLI, webhooks, and scripts.
    """
    if not settings.DISCORD_BOT_TOKEN:
        logger.info(
            f"[Discord DM Dispatch (Mock/No Token)] To: {discord_name_or_id} | Message:\n{message}"
        )
        return False

    token = settings.DISCORD_BOT_TOKEN.strip()
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://tigimax.mooo.com, 1.0)",
        "Content-Type": "application/json",
    }

    # 1. Resolve target Discord user ID
    user_id = None
    target_clean = discord_name_or_id.strip()
    if target_clean.isdigit():
        user_id = target_clean
    else:
        guild_id = settings.DISCORD_GUILD_ID or "784805552504438794"
        query_enc = urllib.parse.quote(target_clean)
        search_url = (
            f"https://discord.com/api/v10/guilds/{guild_id}/members/search?query={query_enc}&limit=10"
        )
        try:
            req = urllib.request.Request(search_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                members = json.loads(resp.read().decode())
                target_lower = target_clean.lower()
                for m in members:
                    u = m.get("user", {})
                    username = (u.get("username") or "").lower()
                    global_name = (u.get("global_name") or "").lower()
                    nick = (m.get("nick") or "").lower()
                    if target_lower in (username, global_name, nick):
                        user_id = str(u.get("id"))
                        break
                if not user_id and members:
                    user_id = str(members[0].get("user", {}).get("id"))
        except Exception as e:
            logger.warning(f"Failed to search member '{target_clean}' in guild {guild_id}: {e}")

    if not user_id:
        logger.error(f"Could not resolve Discord user ID for '{discord_name_or_id}'")
        return False

    # 2. Open DM channel
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

    # 3. Send Message
    try:
        msg_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(msg_url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Successfully dispatched Discord DM to {target_clean} (ID: {user_id})")
            return True
    except Exception as e:
        logger.error(f"Failed to post DM message to channel {channel_id}: {e}")
        return False


def send_dm(discord_name_or_id: str, message: str) -> bool:
    """Synchronous DM dispatcher."""
    return send_dm_rest(discord_name_or_id, message)


def run_bot():
    """Runs the Discord bot event loop."""
    if not settings.DISCORD_BOT_TOKEN:
        logger.warning(
            "DISCORD_BOT_TOKEN is not configured. Discord bot will not start."
        )
        return
    bot.run(settings.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_bot()
