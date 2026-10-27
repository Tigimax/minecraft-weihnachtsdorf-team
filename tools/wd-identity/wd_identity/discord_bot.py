"""
Discord bot for Weihnachtsdorf 7.
Features:
- Slash commands: /passwort-reset, /status, /links
- Direct message dispatcher for invitations and recovery links
- Welcome guide DM for newly onboarded team members
"""

import asyncio
import logging
from typing import Optional
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
        synced = await tree.sync()
        logger.info(f"Synced {len(synced)} application commands.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")


async def send_dm_async(discord_name_or_id: str, message: str) -> bool:
    """Asynchronously sends a direct message to a user."""
    if not bot.is_ready():
        logger.warning("Discord bot is not connected. Skipping DM dispatch.")
        return False

    target_user: Optional[discord.User] = None

    # Try numeric ID
    if discord_name_or_id.isdigit():
        try:
            target_user = await bot.fetch_user(int(discord_name_or_id))
        except Exception:
            pass

    # Search among guilds if not found
    if not target_user:
        for guild in bot.guilds:
            for member in guild.members:
                if (
                    member.name.lower() == discord_name_or_id.lower()
                    or f"{member.name}#{member.discriminator}".lower()
                    == discord_name_or_id.lower()
                ):
                    target_user = member
                    break
            if target_user:
                break

    if not target_user:
        logger.warning(f"Could not find Discord user '{discord_name_or_id}'")
        return False

    try:
        dm_channel = await target_user.create_dm()
        await dm_channel.send(message)
        logger.info(f"Successfully sent DM to {target_user}")
        return True
    except Exception as e:
        logger.error(f"Failed to send DM to {target_user}: {e}")
        return False


def send_dm(discord_name_or_id: str, message: str) -> bool:
    """Synchronous wrapper for sending DMs."""
    if not settings.DISCORD_BOT_TOKEN or not bot.is_ready():
        logger.info(
            f"[Discord DM Dispatch (Offline/Mock)] To: {discord_name_or_id} | Message:\n{message}"
        )
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_dm_async(discord_name_or_id, message), bot.loop
        )
        return future.result(timeout=10)
    except Exception as e:
        logger.error(f"Error in send_dm: {e}")
        return False


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
