"""
Discord bot for Weihnachtsdorf 7.
Features:
- Slash commands: /passwort-reset, /status, /links, /anmelden, /entbannung, /ban-spieler, /pardon-spieler
- Interactive candidate applications (#anmeldung) and unban appeals (#entbannung)
- Admin review dashboard (#admin-anmeldungen) with Accept / Reject buttons & cooldowns
- BlueMap & Minecraft ban integration
- Direct message dispatcher for invitations and recovery links
- Welcome guide DM for newly onboarded team members & players
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Optional
import urllib.parse
import urllib.request
import discord
from discord import app_commands
from discord.ext import commands

from wd_identity import applications
from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


def is_admin_user(user: discord.Member | discord.User) -> bool:
    """Checks whether the user has administrator privileges or Admin role."""
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    admin_role_id = str(getattr(settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371"))
    for role in user.roles:
        if str(role.id) == admin_role_id or role.name.lower() in ("admin", "administrator"):
            return True
    return False


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


def get_player_welcome_message(username: str, ign: str, is_launched: bool = False) -> str:
    if is_launched:
        whitelist_status = "✅ Du bist ab sofort auf der Server-Whitelist freigeschaltet! Du kannst direkt joinen."
    else:
        whitelist_status = "⏳ **Pre-Launch:** Dein Account ist startklar registriert! Die Freischaltung auf der Minecraft-Whitelist erfolgt automatisch zum offiziellen Projektstart. 🎅"

    return f"""🎄 **Willkommen bei Weihnachtsdorf 7, {username}!** 🎄

Deine Registrierung als **Spieler** war erfolgreich.

📌 **Wichtige Zugänge & Portale:**
• **Authentik SSO Portal:** {settings.AUTHENTIK_BASE_URL} (Profil, Passwort & 2FA verwalten)
• **BlueMap Live-Karte:** https://map.tigimax.mooo.com
• **Minecraft Server:** `tigimax.mooo.com` (Purpur 26.3)

🎮 **Dein Minecraft Profil:**
• Ingame-Name: `{ign}`
• Whitelist-Status: {whitelist_status}

🔒 **Sicherheitsrichtlinien:**
• 2FA (TOTP) ist für deinen Account aktiv geschützt.
• Bei Passwort-Verlust kannst du jederzeit hier im Discord `/passwort-reset` nutzen.

Wir freuen uns auf ein unvergessliches Event mit dir! ✨"""


# ---------------------------------------------------------------------------
# Modals for User Submissions and Admin Reviews
# ---------------------------------------------------------------------------

class ApplicationModal(discord.ui.Modal, title="Weihnachtsdorf 7 • Anmeldung"):
    message_input = discord.ui.TextInput(
        label="Deine Nachricht an das Team",
        style=discord.TextStyle.paragraph,
        placeholder="Wer bist du? Warum möchtest du beim Weihnachtsdorf 7 mitmachen?",
        min_length=10,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await handle_application_submit(interaction, self.message_input.value)


class UnbanModal(discord.ui.Modal, title="Entbannung / Einspruch"):
    statement_input = discord.ui.TextInput(
        label="Deine Stellungnahme / Begründung",
        style=discord.TextStyle.paragraph,
        placeholder="Warum sollte deine Sperre aufgehoben bzw. deine Bewerbung erneut geprüft werden?",
        min_length=15,
        max_length=1500,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await handle_unban_submit(interaction, self.statement_input.value)


class AdminRejectModal(discord.ui.Modal):
    def __init__(self, app_id: int):
        super().__init__(title=f"Bewerbung #{app_id} ablehnen")
        self.app_id = app_id
        self.reason_input = discord.ui.TextInput(
            label="Begründung (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Optional: Begründung angeben (wird dem Nutzer per DM gesendet)...",
            required=False,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await handle_admin_reject_submit(interaction, self.app_id, self.reason_input.value)


class AdminAppealRejectModal(discord.ui.Modal):
    def __init__(self, app_id: int):
        super().__init__(title=f"Einspruch #{app_id} ablehnen")
        self.app_id = app_id
        self.days_input = discord.ui.TextInput(
            label="Sperrfrist in Tagen (-1 für Dauerhaft)",
            default="14",
            placeholder="z. B. 7, 14, 30, 90 oder -1 für permanente Sperre",
            max_length=5,
            required=True,
        )
        self.reason_input = discord.ui.TextInput(
            label="Begründung der Ablehnung (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Grund für die Ablehnung des Entbannungsantrags...",
            required=False,
            max_length=500,
        )
        self.add_item(self.days_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await handle_admin_appeal_reject_submit(
            interaction, self.app_id, self.days_input.value, self.reason_input.value
        )


# ---------------------------------------------------------------------------
# Persistent UI Views
# ---------------------------------------------------------------------------

class ApplyStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Jetzt bewerben",
        style=discord.ButtonStyle.success,
        emoji="📝",
        custom_id="wd_apply_start_btn",
    )
    async def btn_apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await trigger_application_modal(interaction)


class AppealStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Einspruch / Entbannung beantragen",
        style=discord.ButtonStyle.secondary,
        emoji="⚖️",
        custom_id="wd_appeal_start_btn",
    )
    async def btn_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await trigger_unban_modal(interaction)


class AdminActionRow(discord.ui.View):
    """Action row with Accept and Reject buttons for admin dashboard."""
    def __init__(self, app_id: int, is_appeal: bool = False):
        super().__init__(timeout=None)
        prefix = "appeal" if is_appeal else "app"
        accept_label = "Entbannen / Annehmen" if is_appeal else "Annehmen"
        reject_label = "Ablehnen & Sperre setzen" if is_appeal else "Ablehnen"

        self.add_item(
            discord.ui.Button(
                label=accept_label,
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"{prefix}:accept:{app_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label=reject_label,
                style=discord.ButtonStyle.danger,
                emoji="❌",
                custom_id=f"{prefix}:reject:{app_id}",
            )
        )


# ---------------------------------------------------------------------------
# Interaction Logic Helpers
# ---------------------------------------------------------------------------

async def trigger_application_modal(interaction: discord.Interaction):
    """Validates user eligibility and displays the application modal."""
    status = applications.get_user_status(str(interaction.user.id))

    if status["is_banned"]:
        await interaction.response.send_message(
            "❌ **Du bist derzeit auf dem Weihnachtsdorf-Server gesperrt.**\n"
            "Bitte nutze den Channel `#entbannung`, um eine Stellungnahme bzw. einen Entbannungsantrag einzureichen.",
            ephemeral=True,
        )
        return

    if status["in_cooldown"]:
        await interaction.response.send_message(
            f"⏳ **Du bist derzeit für weitere Anträge gesperrt.**\n"
            f"Dein Cooldown läuft noch **{status['cooldown_str']}**.",
            ephemeral=True,
        )
        return

    if status["is_rejected"]:
        await interaction.response.send_message(
            "❌ **Deine vorherige Bewerbung wurde abgelehnt.**\n"
            "Du kannst keinen weiteren initialen Aufnahmeantrag stellen.\n"
            "Falls du Einspruch einlegen möchtest, nutze bitte den Channel `#entbannung`.",
            ephemeral=True,
        )
        return

    if status["has_pending"]:
        await interaction.response.send_message(
            "⚠️ **Du hast bereits eine offene Bewerbung eingereicht!**\n"
            "Bitte gedulde dich, bis ein Administrator über deine Bewerbung entschieden hat.",
            ephemeral=True,
        )
        return

    # Check if already registered in LDAP
    ldap = LDAPClient()
    existing = ldap.search_by_discord(str(interaction.user.id)) or ldap.search_by_discord(interaction.user.name)
    if existing:
        await interaction.response.send_message(
            f"✅ **Du bist bereits für Weihnachtsdorf 7 registriert!** (Account: `{existing['uid']}`)\n"
            f"Nutze `/status`, um deine Zugänge und Berechtigungen einzusehen.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(ApplicationModal())


async def trigger_unban_modal(interaction: discord.Interaction):
    """Validates unban eligibility and displays the unban appeal modal."""
    status = applications.get_user_status(str(interaction.user.id))

    if status["in_cooldown"]:
        await interaction.response.send_message(
            f"⏳ **Du kannst derzeit keinen weiteren Antrag stellen.**\n"
            f"Dein Cooldown läuft noch **{status['cooldown_str']}**.\n\n"
            f"*Sobald diese Frist abgelaufen ist, kannst du erneut einen Antrag stellen.*",
            ephemeral=True,
        )
        return

    if status["has_pending"]:
        await interaction.response.send_message(
            "⚠️ **Du hast bereits einen offenen Antrag eingereicht!**\n"
            "Das Admin-Team prüft deine Anfrage derzeit. Bitte habe noch etwas Geduld.",
            ephemeral=True,
        )
        return

    if not status["is_rejected"] and not status["is_banned"]:
        await interaction.response.send_message(
            "ℹ️ **Du bist weder abgelehnt noch gesperrt!**\n"
            "Du kannst dich ganz normal im Channel `#anmeldung` für die Teilnahme bewerben.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(UnbanModal())


async def handle_application_submit(interaction: discord.Interaction, message_text: str):
    """Saves candidate application and dispatches review embed to admin channel."""
    app_id = applications.create_application(
        discord_id=str(interaction.user.id),
        discord_username=interaction.user.name,
        app_type="initial",
        message=message_text,
    )

    await interaction.response.send_message(
        "✅ **Deine Bewerbung wurde erfolgreich an das Admin-Team übermittelt!**\n"
        "Du wirst hier per Direktnachricht (DM) benachrichtigt, sobald über deinen Antrag entschieden wurde.",
        ephemeral=True,
    )

    admin_channel = await resolve_admin_channel(interaction.guild)
    if admin_channel:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        embed = discord.Embed(
            title=f"📝 Neuer Aufnahmeantrag • ID #{app_id}",
            description=f"Bewerbung von {interaction.user.mention} (`{interaction.user.name}`)",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Bewerber", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
        embed.add_field(name="Antragstyp", value="Initialer Aufnahmeantrag", inline=True)
        embed.add_field(name="Eingereicht am", value=f"<t:{now_ts}:F> (<t:{now_ts}:R>)", inline=False)
        embed.add_field(name="Bewerbungstext", value=f"```{message_text}```", inline=False)
        embed.set_footer(text="Weihnachtsdorf 7 Identity System")

        msg = await admin_channel.send(embed=embed, view=AdminActionRow(app_id, is_appeal=False))
        applications.set_channel_message_id(app_id, str(msg.id))
    else:
        logger.error("Could not resolve admin review channel to post new application.")


async def handle_unban_submit(interaction: discord.Interaction, statement_text: str):
    """Saves unban appeal and dispatches review embed to admin channel."""
    app_id = applications.create_application(
        discord_id=str(interaction.user.id),
        discord_username=interaction.user.name,
        app_type="unban",
        message=statement_text,
    )

    await interaction.response.send_message(
        "✅ **Dein Entbannungs- / Einspruchsantrag wurde eingereicht.**\n"
        "Das Admin-Team wird ihn prüfen. Du erhältst automatisch per DM Bescheid.",
        ephemeral=True,
    )

    status = applications.get_user_status(str(interaction.user.id))
    history_desc = "Initialer Aufnahmeantrag abgelehnt"
    if status["is_banned"] and status["active_ban"]:
        history_desc = f"Minecraft-Ban: `{status['active_ban'].get('reason')}`"

    admin_channel = await resolve_admin_channel(interaction.guild)
    if admin_channel:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        embed = discord.Embed(
            title=f"⚖️ Neuer Entbannungs- / Einspruchsantrag • ID #{app_id}",
            description=f"Antrag von {interaction.user.mention} (`{interaction.user.name}`)",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Antragsteller", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
        embed.add_field(name="Vorgeschichte", value=history_desc, inline=True)
        embed.add_field(name="Eingereicht am", value=f"<t:{now_ts}:F> (<t:{now_ts}:R>)", inline=False)
        embed.add_field(name="Stellungnahme", value=f"```{statement_text}```", inline=False)
        embed.set_footer(text="Weihnachtsdorf 7 Identity System")

        msg = await admin_channel.send(embed=embed, view=AdminActionRow(app_id, is_appeal=True))
        applications.set_channel_message_id(app_id, str(msg.id))
    else:
        logger.error("Could not resolve admin review channel to post unban appeal.")


async def handle_admin_accept(interaction: discord.Interaction, app_id: int):
    """Admin clicks Accept for an initial application."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Anträge bearbeiten.", ephemeral=True)
        return

    app = applications.get_application(app_id)
    if not app or app["status"] != "pending":
        await interaction.response.send_message("⚠️ Dieser Antrag wurde bereits bearbeitet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    discord_id = str(app["discord_id"])
    discord_name = app["discord_username"]

    # Generate Authentik single-use enrollment link
    authentik = AuthentikClient()
    fixed_data = {
        "discord_id": discord_id,
        "discord_name": discord_name,
        "role": "player",
    }
    invite_link = None
    try:
        clean_user = "".join(c for c in discord_name.lower() if c.isalnum() or c in ("-", "_")) or f"user{discord_id[-4:]}"
        authentik.delete_user(clean_user)
        invite_link = authentik.create_invitation(
            name=f"invite-{clean_user}",
            fixed_data=fixed_data,
        )
    except Exception as e:
        logger.error(f"Failed to generate Authentik invitation for {discord_name}: {e}")

    # Dispatch DM to applicant
    dm_sent = False
    if invite_link:
        dm_message = (
            f"🎅 **Glückwunsch! Deine Anmeldung für Weihnachtsdorf 7 wurde angenommen!** 🎄\n\n"
            f"Du wurdest als **Spieler** zum Event zugelassen.\n"
            f"Bitte schließe deine Registrierung über folgenden persönlichen Einladungslink ab:\n"
            f"👉 {invite_link}\n\n"
            f"📌 **Was dich erwartet:**\n"
            f"• Passwort & 2FA einrichten (zentraler SSO-Account für alle Portale)\n"
            f"• Angabe deines Minecraft Ingame-Namens\n"
            f"• Nach Abschluss ist dein Profil startklar für den Server!\n\n"
            f"*Dieser Einladungslink ist einmalig gültig und verfällt nach 24 Stunden.*"
        )
        dm_sent = send_dm_rest(discord_id, dm_message)

    # Update DB
    applications.accept_application(app_id, str(interaction.user.id), interaction.user.name)

    # Edit embed in admin channel
    if interaction.message:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        new_embed = interaction.message.embeds[0]
        new_embed.color = discord.Color.green()
        new_embed.add_field(
            name="Status",
            value=f"✅ **Angenommen** von {interaction.user.mention} (<t:{now_ts}:R>)\n"
                  f"{'📨 Einladung per DM zugestellt' if dm_sent else '⚠️ DM fehlgeschlagen (bitte Link manuell übermitteln)'}",
            inline=False,
        )
        if not dm_sent and invite_link:
            new_embed.add_field(name="Einladungslink (Backup)", value=f"`{invite_link}`", inline=False)
        await interaction.message.edit(embed=new_embed, view=None)

    msg = f"✅ Antrag #{app_id} angenommen!"
    if invite_link:
        msg += f" Einladungslink {'per DM versendet.' if dm_sent else f': {invite_link}'}"
    await interaction.followup.send(msg, ephemeral=True)


async def handle_admin_reject_submit(interaction: discord.Interaction, app_id: int, reason_text: str):
    """Admin confirms rejection of an initial application."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Anträge bearbeiten.", ephemeral=True)
        return

    app = applications.get_application(app_id)
    if not app or app["status"] != "pending":
        await interaction.response.send_message("⚠️ Dieser Antrag wurde bereits bearbeitet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    clean_reason = (reason_text or "").strip()
    if not clean_reason:
        clean_reason = "Vielen Dank für dein Interesse. Leider können wir deine Bewerbung zum aktuellen Zeitpunkt nicht berücksichtigen."

    applications.reject_application(
        app_id=app_id,
        reviewer_id=str(interaction.user.id),
        reviewer_name=interaction.user.name,
        reason=clean_reason,
        cooldown_days=None,
    )

    dm_message = (
        f"❌ **Deine Anmeldung für Weihnachtsdorf 7 wurde abgelehnt.**\n\n"
        f"**Begründung:**\n{clean_reason}\n\n"
        f"*Hinweis: Du kannst keinen weiteren initialen Aufnahmeantrag stellen. "
        f"Falls du Einspruch einlegen möchtest, kannst du im Channel `#entbannung` einen Antrag einreichen.*"
    )
    send_dm_rest(str(app["discord_id"]), dm_message)

    # Edit admin message
    if interaction.message:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        new_embed = interaction.message.embeds[0]
        new_embed.color = discord.Color.red()
        new_embed.add_field(
            name="Status",
            value=f"❌ **Abgelehnt** von {interaction.user.mention} (<t:{now_ts}:R>)\n**Grund:** {clean_reason}",
            inline=False,
        )
        await interaction.message.edit(embed=new_embed, view=None)

    await interaction.followup.send(f"❌ Antrag #{app_id} abgelehnt und Begründung per DM versendet.", ephemeral=True)


async def handle_admin_appeal_accept(interaction: discord.Interaction, app_id: int):
    """Admin accepts an unban/appeal request."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Anträge bearbeiten.", ephemeral=True)
        return

    app = applications.get_application(app_id)
    if not app or app["status"] != "pending":
        await interaction.response.send_message("⚠️ Dieser Antrag wurde bereits bearbeitet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    discord_id = str(app["discord_id"])
    discord_name = app["discord_username"]

    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(discord_name)
    was_previously_enrolled = user is not None and user.get("minecraftName")

    if was_previously_enrolled:
        ign = user.get("minecraftName")
        # 1. Pardon Minecraft server
        mc.pardon_player(ign)
        # 2. Restore BlueMap access
        authentik.set_user_banned(user["uid"], False)
        # 3. Lift active bans in DB
        applications.lift_ban(discord_id=discord_id, minecraft_ign=ign)
        # 4. DM user
        dm_msg = (
            f"🎉 **Dein Entbannungsantrag für Weihnachtsdorf 7 wurde angenommen!**\n\n"
            f"Deine Sperre auf dem Minecraft-Server (`{ign}`) und der BlueMap Live-Karte wurde aufgehoben.\n"
            f"Willkommen zurück im Weihnachtsdorf! 🎅"
        )
        send_dm_rest(discord_id, dm_msg)
    else:
        # User was rejected initially and had no account yet: Send Authentik invite link
        clean_user = "".join(c for c in discord_name.lower() if c.isalnum() or c in ("-", "_")) or f"user{discord_id[-4:]}"
        authentik.delete_user(clean_user)
        invite_link = authentik.create_invitation(
            name=f"invite-{clean_user}",
            fixed_data={"discord_id": discord_id, "discord_name": discord_name, "role": "player"},
        )
        dm_msg = (
            f"🎉 **Dein Einspruch für Weihnachtsdorf 7 wurde angenommen!** 🎄\n\n"
            f"Du kannst dich ab sofort als **Spieler** registrieren:\n"
            f"👉 {invite_link}\n\n"
            f"*Dieser Link verfällt nach 24 Stunden.*"
        )
        send_dm_rest(discord_id, dm_msg)

    applications.accept_application(app_id, str(interaction.user.id), interaction.user.name)

    if interaction.message:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        new_embed = interaction.message.embeds[0]
        new_embed.color = discord.Color.green()
        detail_msg = f"Minecraft & BlueMap freigeschaltet ({user.get('minecraftName')})" if was_previously_enrolled else "Einladungslink per DM versendet"
        new_embed.add_field(
            name="Status",
            value=f"✅ **Entbannt / Angenommen** von {interaction.user.mention} (<t:{now_ts}:R>)\n{detail_msg}",
            inline=False,
        )
        await interaction.message.edit(embed=new_embed, view=None)

    await interaction.followup.send(f"✅ Entbannungsantrag #{app_id} angenommen!", ephemeral=True)


async def handle_admin_appeal_reject_submit(
    interaction: discord.Interaction, app_id: int, days_str: str, reason_text: str
):
    """Admin rejects an unban appeal and applies cooldown."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Anträge bearbeiten.", ephemeral=True)
        return

    app = applications.get_application(app_id)
    if not app or app["status"] != "pending":
        await interaction.response.send_message("⚠️ Dieser Antrag wurde bereits bearbeitet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Parse cooldown days
    try:
        cooldown_days = int(days_str.strip())
    except ValueError:
        cooldown_days = 14

    clean_reason = (reason_text or "").strip()
    if not clean_reason:
        clean_reason = "Dein Entbannungsantrag wurde nach Prüfung abgelehnt."

    rejected = applications.reject_application(
        app_id=app_id,
        reviewer_id=str(interaction.user.id),
        reviewer_name=interaction.user.name,
        reason=clean_reason,
        cooldown_days=cooldown_days,
    )

    status = applications.get_user_status(str(app["discord_id"]))
    if cooldown_days < 0:
        sperre_text = "**Dauerhaft (Permanent):** Es können keine weiteren Anträge mehr gestellt werden."
    else:
        sperre_text = f"**{cooldown_days} Tage** ({status['cooldown_str']})"

    dm_msg = (
        f"❌ **Dein Entbannungs- / Einspruchsantrag für Weihnachtsdorf 7 wurde abgelehnt.**\n\n"
        f"**Begründung:**\n{clean_reason}\n\n"
        f"⏳ **Sperrfrist für neue Anträge:**\n{sperre_text}\n\n"
        f"*Innerhalb dieses Zeitraums werden keine weiteren Anträge entgegengenommen.*"
    )
    send_dm_rest(str(app["discord_id"]), dm_msg)

    if interaction.message:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        new_embed = interaction.message.embeds[0]
        new_embed.color = discord.Color.red()
        new_embed.add_field(
            name="Status",
            value=f"❌ **Abgelehnt** von {interaction.user.mention} (<t:{now_ts}:R>)\n"
                  f"**Sperre:** {sperre_text}\n**Grund:** {clean_reason}",
            inline=False,
        )
        await interaction.message.edit(embed=new_embed, view=None)

    await interaction.followup.send(f"❌ Einspruch #{app_id} abgelehnt mit Sperre ({sperre_text}).", ephemeral=True)


# ---------------------------------------------------------------------------
# Global Interaction Listener for Dynamic Buttons
# ---------------------------------------------------------------------------

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")

        if custom_id == "wd_apply_start_btn":
            await trigger_application_modal(interaction)
            return

        if custom_id == "wd_appeal_start_btn":
            await trigger_unban_modal(interaction)
            return

        if custom_id.startswith("app:accept:"):
            app_id = int(custom_id.split(":")[-1])
            await handle_admin_accept(interaction, app_id)
            return

        if custom_id.startswith("app:reject:"):
            app_id = int(custom_id.split(":")[-1])
            if not is_admin_user(interaction.user):
                await interaction.response.send_message("❌ Nur Administratoren können Anträge bearbeiten.", ephemeral=True)
                return
            await interaction.response.send_modal(AdminRejectModal(app_id))
            return

        if custom_id.startswith("appeal:accept:"):
            app_id = int(custom_id.split(":")[-1])
            await handle_admin_appeal_accept(interaction, app_id)
            return

        if custom_id.startswith("appeal:reject:"):
            app_id = int(custom_id.split(":")[-1])
            if not is_admin_user(interaction.user):
                await interaction.response.send_message("❌ Nur Administratoren können Anträge bearbeiten.", ephemeral=True)
                return
            await interaction.response.send_modal(AdminAppealRejectModal(app_id))
            return


# ---------------------------------------------------------------------------
# Channel Discovery & Setup
# ---------------------------------------------------------------------------

async def resolve_admin_channel(guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
    """Resolves the admin review channel by configured ID or matching name."""
    if not guild:
        guild = bot.get_guild(int(settings.DISCORD_GUILD_ID)) if settings.DISCORD_GUILD_ID else None
    if not guild:
        return None

    admin_channel_id = getattr(settings, "DISCORD_CHANNEL_ADMIN_ID", "")
    if admin_channel_id and admin_channel_id.isdigit():
        c = guild.get_channel(int(admin_channel_id))
        if isinstance(c, discord.TextChannel):
            return c

    # Search by matching name
    candidates = ["admin-anmeldungen", "anmeldungen-admin", "anträge-admin", "mod-chat"]
    for name in candidates:
        for ch in guild.text_channels:
            if ch.name.lower() == name:
                return ch
    return None


async def ensure_channels_and_pins():
    """
    Ensures #anmeldung, #entbannung, and #admin-anmeldungen exist
    under category Weihnachtsdorf 7 and have pinned onboarding views.
    """
    if not settings.DISCORD_GUILD_ID:
        return

    guild = bot.get_guild(int(settings.DISCORD_GUILD_ID))
    if not guild:
        logger.warning(f"Configured guild {settings.DISCORD_GUILD_ID} not found in bot cache.")
        return

    category_id = getattr(settings, "DISCORD_CATEGORY_WD7_ID", "1561436206577750138")
    category = guild.get_channel(int(category_id)) if category_id and category_id.isdigit() else None

    # Bot permissions check
    me = guild.me
    can_manage_channels = me.guild_permissions.manage_channels or me.guild_permissions.administrator

    # 1. Anmeldung Channel
    anmeldung_ch = None
    for ch in guild.text_channels:
        if ch.name.lower() in ("anmeldung", "anmeldungen") and (not category or ch.category_id == category.id):
            anmeldung_ch = ch
            break

    if not anmeldung_ch and can_manage_channels:
        try:
            anmeldung_ch = await guild.create_text_channel(
                name="anmeldung",
                category=category,
                topic="Offizieller Anmelde-Kanal für Teilnehmer an Weihnachtsdorf 7.",
            )
            logger.info(f"Created channel #{anmeldung_ch.name} (ID: {anmeldung_ch.id})")
        except Exception as e:
            logger.warning(f"Could not create #anmeldung channel: {e}")

    if anmeldung_ch:
        # Check if pinned guide message exists
        pins = [p async for p in anmeldung_ch.pins()]
        has_guide = any(p.author.id == bot.user.id and "Anmeldung für Weihnachtsdorf 7" in (p.embeds[0].title if p.embeds else "") for p in pins)
        if not has_guide:
            embed = discord.Embed(
                title="🎄 Anmeldung für Weihnachtsdorf 7 🎄",
                description=(
                    "Willkommen im weihnachtlichen SMP-Projekt! Hier kannst du deine Teilnahme beantragen.\n\n"
                    "📝 **So funktioniert die Anmeldung:**\n"
                    "1. Klicke unten auf den Button **„Jetzt bewerben“** oder nutze den Befehl `/anmelden`.\n"
                    "2. Gib eine kurze Nachricht an das Team ein (wer du bist, warum du mitmachen möchtest).\n"
                    "3. Dein Antrag wird vertraulich an die Administratoren übermittelt.\n"
                    "4. Nach der Annahme erhältst du eine Direktnachricht (DM) mit deinem persönlichen Registrierungs-Link.\n\n"
                    "*(Deinen Minecraft-Namen gibst du erst nach der Zusage bei der Accounterstellung an!)*"
                ),
                color=discord.Color.gold(),
            )
            embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")
            msg = await anmeldung_ch.send(embed=embed, view=ApplyStartView())
            try:
                await msg.pin()
            except Exception:
                pass

    # 2. Entbannung Channel
    entbannung_ch = None
    for ch in guild.text_channels:
        if ch.name.lower() in ("entbannung", "entbannungsanträge", "entbannungsantrag") and (not category or ch.category_id == category.id):
            entbannung_ch = ch
            break

    if not entbannung_ch and can_manage_channels:
        try:
            entbannung_ch = await guild.create_text_channel(
                name="entbannung",
                category=category,
                topic="Kanal für Einsprüche und Entbannungsanträge.",
            )
            logger.info(f"Created channel #{entbannung_ch.name} (ID: {entbannung_ch.id})")
        except Exception as e:
            logger.warning(f"Could not create #entbannung channel: {e}")

    if entbannung_ch:
        pins = [p async for p in entbannung_ch.pins()]
        has_guide = any(p.author.id == bot.user.id and "Entbannungs- & Einspruchsanträge" in (p.embeds[0].title if p.embeds else "") for p in pins)
        if not has_guide:
            embed = discord.Embed(
                title="⚖️ Entbannungs- & Einspruchsanträge ⚖️",
                description=(
                    "Falls deine Bewerbung abgelehnt wurde oder dein Zugang auf dem Minecraft-Server gesperrt ist, "
                    "hast du hier die Möglichkeit, eine Stellungnahme einzureichen.\n\n"
                    "📌 **Hinweise zur Antragstellung:**\n"
                    "• Beschreibe sachlich und ehrlich, warum dein Fall neu geprüft werden sollte.\n"
                    "• Wenn ein Antrag wiederholt abgelehnt wird, kann eine Sperrfrist (Cooldown) verhängt werden.\n"
                    "• Während eines aktiven Cooldowns können keine weiteren Anträge gestellt werden."
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")
            msg = await entbannung_ch.send(embed=embed, view=AppealStartView())
            try:
                await msg.pin()
            except Exception:
                pass

    # 3. Admin Review Channel
    admin_ch = await resolve_admin_channel(guild)
    if not admin_ch and can_manage_channels:
        try:
            admin_role = guild.get_role(int(getattr(settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371")))
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True),
            }
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            admin_ch = await guild.create_text_channel(
                name="admin-anmeldungen",
                category=category,
                overwrites=overwrites,
                topic="Prüfung eingehender Aufnahme- und Entbannungsanträge.",
            )
            logger.info(f"Created admin channel #{admin_ch.name} (ID: {admin_ch.id})")
        except Exception as e:
            logger.warning(f"Could not create #admin-anmeldungen channel: {e}")


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

@tree.command(
    name="anmelden",
    description="Reiche deine Bewerbung / Aufnahmeantrag für Weihnachtsdorf 7 ein.",
)
async def cmd_anmelden(interaction: discord.Interaction):
    await trigger_application_modal(interaction)


@tree.command(
    name="entbannung",
    description="Stelle einen Entbannungs- oder Einspruchsantrag.",
)
async def cmd_entbannung(interaction: discord.Interaction):
    await trigger_unban_modal(interaction)


@tree.command(
    name="ban-spieler",
    description="Sperrt einen Spieler vom Minecraft-Server und der BlueMap (Admin).",
)
@app_commands.describe(
    target="Discord-Nutzer (@User oder ID) oder Ingame-Name",
    grund="Begründung für die Sperre",
)
async def cmd_ban_player(interaction: discord.Interaction, target: str, grund: Optional[str] = None):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können diesen Befehl ausführen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    clean_target = target.strip().replace("<@", "").replace(">", "").replace("!", "")
    clean_reason = (grund or "Regelverstoß").strip()

    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    user = None
    if clean_target.isdigit():
        user = ldap.search_by_discord(clean_target)
    if not user:
        user = ldap.get_user(clean_target) or ldap.search_by_minecraft(clean_target)

    discord_id = None
    ign = None
    uid = None

    if user:
        discord_id = user.get("discordId")
        ign = user.get("minecraftName")
        uid = user["uid"]
    else:
        # Target might be pure Minecraft IGN without LDAP
        ign = clean_target

    mc_banned = False
    if ign:
        mc_banned = mc.ban_player(ign, clean_reason)

    bluemap_blocked = False
    if uid:
        bluemap_blocked = authentik.set_user_banned(uid, True)

    applications.record_ban(
        discord_id=discord_id,
        minecraft_ign=ign,
        banned_by=interaction.user.name,
        reason=clean_reason,
    )

    dm_sent = False
    if discord_id:
        dm_msg = (
            f"⛔ **Du wurdest vom Projekt Weihnachtsdorf 7 gesperrt.**\n\n"
            f"**Grund:** {clean_reason}\n"
            f"**Betroffene Dienste:** Minecraft-Server (`{ign or 'Account'}`) & BlueMap Live-Karte\n\n"
            f"*Dein zentraler Authentik-Account bleibt für Passwortresets aktiv.*\n"
            f"*Falls du Einspruch einlegen möchtest, kannst du im Channel `#entbannung` einen Antrag stellen.*"
        )
        dm_sent = send_dm_rest(discord_id, dm_msg)

    res_msg = (
        f"✅ **Sperre erfolgreich verhängt für `{ign or clean_target}`!**\n"
        f"• Minecraft Server Ban: {'✅ Ausgeführt' if mc_banned else '⚠️ Fehler oder kein IGN'}\n"
        f"• BlueMap-Zugang gesperrt: {'✅ Gesperrt' if bluemap_blocked else '⚠️ Kein Authentik-User verknüpft'}\n"
        f"• DM-Zustellung: {'✅ Zugestellt' if dm_sent else 'ℹ️ Keine DM gesendet'}"
    )
    await interaction.followup.send(res_msg, ephemeral=True)


@tree.command(
    name="pardon-spieler",
    description="Hebt die Sperre eines Spielers auf Minecraft & BlueMap auf (Admin).",
)
@app_commands.describe(target="Discord-Nutzer (@User oder ID) oder Ingame-Name")
async def cmd_pardon_player(interaction: discord.Interaction, target: str):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können diesen Befehl ausführen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    clean_target = target.strip().replace("<@", "").replace(">", "").replace("!", "")

    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    user = None
    if clean_target.isdigit():
        user = ldap.search_by_discord(clean_target)
    if not user:
        user = ldap.get_user(clean_target) or ldap.search_by_minecraft(clean_target)

    discord_id = None
    ign = None
    uid = None

    if user:
        discord_id = user.get("discordId")
        ign = user.get("minecraftName")
        uid = user["uid"]
    else:
        ign = clean_target

    if ign:
        mc.pardon_player(ign)

    if uid:
        authentik.set_user_banned(uid, False)

    applications.lift_ban(discord_id=discord_id, minecraft_ign=ign)

    if discord_id:
        dm_msg = (
            f"🎉 **Deine Sperre für Weihnachtsdorf 7 wurde aufgehoben!**\n"
            f"Du kannst den Minecraft-Server und die BlueMap Live-Karte wieder nutzen. Willkommen zurück!"
        )
        send_dm_rest(discord_id, dm_msg)

    await interaction.followup.send(
        f"✅ Spieler `{ign or clean_target}` wurde erfolgreich entbannt (Minecraft & BlueMap freigeschaltet).",
        ephemeral=True,
    )


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

    user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(discord_tag)
    if not user:
        await interaction.followup.send(
            "❌ Dein Discord-Account ist mit keinem Weihnachtsdorf-Account verknüpft. Bitte kontaktiere einen Administrator.",
            ephemeral=True,
        )
        return

    recovery_link = authentik.create_recovery_link(user["uid"])
    if not recovery_link:
        await interaction.followup.send(
            f"⚠️ Es konnte kein Wiederherstellungslink für `{user['uid']}` generiert werden. Bitte wende dich an @Tigimax.",
            ephemeral=True,
        )
        return

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
    players = ldap.get_players()
    admin_uids = [a["uid"] for a in admins]
    mod_uids = [m["uid"] for m in mods]
    player_uids = [p["uid"] for p in players]

    mc = MinecraftSyncClient()
    is_live = mc.is_project_launched()

    if user["uid"] in admin_uids:
        role = "Administrator 🔴"
        whitelist_info = "Aktiviert (Staff)"
    elif user["uid"] in mod_uids:
        role = "Moderator 🟡"
        whitelist_info = "Aktiviert (Staff)"
    elif user["uid"] in player_uids:
        role = "Spieler 🟢"
        if is_live:
            whitelist_info = "Aktiviert (Live)"
        else:
            whitelist_info = "Vorab registriert (Freischaltung zum Projektstart 🎅)"
    else:
        role = "Benutzer ⚪"
        whitelist_info = "Keine Rolle zugewiesen"

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
        name="Minecraft Whitelist",
        value=whitelist_info,
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

    # Register persistent views so buttons work across restarts
    bot.add_view(ApplyStartView())
    bot.add_view(AppealStartView())

    # Sync slash commands
    try:
        if settings.DISCORD_GUILD_ID:
            guild_obj = discord.Object(id=int(settings.DISCORD_GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced_guild = await tree.sync(guild=guild_obj)
            logger.info(f"Synced {len(synced_guild)} slash commands to guild {settings.DISCORD_GUILD_ID}")
        synced = await tree.sync()
        logger.info(f"Synced {len(synced)} application commands globally.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

    # Discover and set up application channels and pinned guide views
    try:
        await ensure_channels_and_pins()
    except Exception as e:
        logger.error(f"Failed during channel discovery/setup: {e}")


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

    user_id = None
    target_clean = discord_name_or_id.strip()
    if target_clean.isdigit():
        user_id = target_clean
    else:
        guild_ids = []
        try:
            guilds_req = urllib.request.Request(
                "https://discord.com/api/v10/users/@me/guilds", headers=headers
            )
            with urllib.request.urlopen(guilds_req, timeout=5) as g_resp:
                all_guilds = json.loads(g_resp.read().decode())
                guild_ids = [g["id"] for g in all_guilds]
        except Exception:
            pass

        if not guild_ids:
            fallback = settings.DISCORD_GUILD_ID or "909437200125538364"
            guild_ids = [fallback, "784805552504438794"]

        query_enc = urllib.parse.quote(target_clean)
        target_lower = target_clean.lower()
        for gid in guild_ids:
            search_url = (
                f"https://discord.com/api/v10/guilds/{gid}/members/search?query={query_enc}&limit=10"
            )
            try:
                req = urllib.request.Request(search_url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    members = json.loads(resp.read().decode())
                    for m in members:
                        u = m.get("user", {})
                        username = (u.get("username") or "").lower()
                        global_name = (u.get("global_name") or "").lower()
                        nick = (m.get("nick") or "").lower()
                        if target_lower in (username, global_name, nick):
                            user_id = str(u.get("id"))
                            break
                    if not user_id and members:
                        for m in members:
                            u = m.get("user", {})
                            if target_lower in (u.get("username") or "").lower():
                                user_id = str(u.get("id"))
                                break
                if user_id:
                    break
            except Exception as e:
                logger.warning(f"Failed to search member '{target_clean}' in guild {gid}: {e}")

    if not user_id:
        logger.error(f"Could not resolve Discord user ID for '{discord_name_or_id}'")
        return False

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
