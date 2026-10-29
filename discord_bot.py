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
from discord.ext import commands, tasks

from wd_identity import applications
from wd_identity.audit_logger import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_PURPLE,
    COLOR_SUCCESS,
    COLOR_WARNING,
    log_admin_event,
)
from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.invitations import create_user_invitation
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.membership import restore_user_privileges, revoke_user_privileges
from wd_identity.playerlist import sync_playerlist_bot, sync_playerlist_rest
from wd_identity.utils import send_dm, send_dm_rest

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
# Persistent UI Views & Modals
# ---------------------------------------------------------------------------

class ManualInviteModal(discord.ui.Modal, title="Weihnachtsdorf 7 • Manuelle Einladung"):
    target_input = discord.ui.TextInput(
        label="Discord-Nutzer (@User, ID oder Name)",
        placeholder="z.B. @Tigimax oder 330995438255603713",
        required=True,
        max_length=100,
    )
    role_input = discord.ui.TextInput(
        label="Rolle (player, mod, admin)",
        default="player",
        required=True,
        max_length=10,
    )
    ign_input = discord.ui.TextInput(
        label="Minecraft Ingame-Name (optional)",
        placeholder="z.B. Tigimax",
        required=False,
        max_length=20,
    )
    username_input = discord.ui.TextInput(
        label="Wunsch-Benutzername (optional)",
        placeholder="z.B. tigimax (Standard: aus Discord abgeleitet)",
        required=False,
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await handle_manual_invite_submit(
            interaction,
            self.target_input.value.strip(),
            self.role_input.value.strip(),
            self.ign_input.value.strip() or None,
            self.username_input.value.strip() or None,
        )


class ManualInviteStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Nutzer einladen",
        style=discord.ButtonStyle.success,
        emoji="✉️",
        custom_id="wd_manual_invite_start_btn",
    )
    async def btn_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können Einladungen erstellen.", ephemeral=True)
            return
        await interaction.response.send_modal(ManualInviteModal())

    @discord.ui.button(
        label="Spielerliste aktualisieren",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="wd_manual_invite_refresh_list_btn",
    )
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await sync_playerlist_bot(bot, force=True)
        if ok:
            await interaction.followup.send("✅ Die `#spielerliste` wurde erfolgreich mit allen Systemen synchronisiert.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Fehler bei der Synchronisierung der Spielerliste.", ephemeral=True)


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
    if interaction.response.is_done():
        return
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
    if interaction.response.is_done():
        return
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

        log_admin_event(
            title="📝 Neue Anmeldung eingereicht",
            description=f"Nutzer {interaction.user.mention} (`{interaction.user.name}`) hat eine Teilnahme-Bewerbung eingereicht (Antrag `#{app_id}`).",
            color=COLOR_INFO,
            fields=[{"name": "Bewerbungstext", "value": message_text[:1000]}],
            author_name=interaction.user.name,
        )
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

        log_admin_event(
            title="⚖️ Entbannungsantrag eingereicht",
            description=f"Nutzer {interaction.user.mention} (`{interaction.user.name}`) hat eine Stellungnahme eingereicht (Antrag `#{app_id}`).",
            color=COLOR_WARNING,
            fields=[{"name": "Stellungnahme", "value": statement_text[:1000]}],
            author_name=interaction.user.name,
        )
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

    log_admin_event(
        title="✅ Anmeldung angenommen",
        description=f"Antrag `#{app_id}` von `<@{discord_id}>` (`{discord_name}`) wurde angenommen.",
        color=COLOR_SUCCESS,
        fields=[
            {"name": "Bearbeitet von", "value": interaction.user.mention, "inline": True},
            {"name": "DM-Zustellung", "value": "✅ Erfolgreich" if dm_sent else "⚠️ Fehlgeschlagen (Link manuell nötig)", "inline": True},
            {"name": "Einladungslink (Backup)", "value": f"`{invite_link}`" if invite_link else "-", "inline": False},
        ],
        author_name=interaction.user.name,
    )

    try:
        await sync_playerlist_bot(bot, force=True)
    except Exception:
        pass


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

    log_admin_event(
        title="❌ Anmeldung abgelehnt",
        description=f"Antrag `#{app_id}` von `<@{app['discord_id']}>` wurde abgelehnt.",
        color=COLOR_ERROR,
        fields=[
            {"name": "Bearbeitet von", "value": interaction.user.mention, "inline": True},
            {"name": "Begründung", "value": clean_reason, "inline": False},
        ],
        author_name=interaction.user.name,
    )


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

    log_admin_event(
        title="🕊️ Entbannungsantrag angenommen",
        description=f"Einspruch `#{app_id}` von `<@{discord_id}>` wurde angenommen. Spieler wurde freigeschaltet.",
        color=COLOR_SUCCESS,
        fields=[
            {"name": "Bearbeitet von", "value": interaction.user.mention, "inline": True},
            {"name": "Status", "value": detail_msg, "inline": True},
        ],
        author_name=interaction.user.name,
    )


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

    log_admin_event(
        title="⛔ Entbannungsantrag abgelehnt",
        description=f"Einspruch `#{app_id}` von `<@{app['discord_id']}>` wurde abgelehnt.",
        color=COLOR_ERROR,
        fields=[
            {"name": "Bearbeitet von", "value": interaction.user.mention, "inline": True},
            {"name": "Begründung", "value": clean_reason, "inline": False},
            {"name": "Sperrfrist", "value": sperre_text, "inline": True},
        ],
        author_name=interaction.user.name,
    )


async def handle_manual_invite_submit(
    interaction: discord.Interaction,
    target_discord: str,
    role: str,
    ign: Optional[str] = None,
    username: Optional[str] = None,
):
    """Executes a manual invitation requested by an admin via modal or slash command."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Einladungen erstellen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        res = create_user_invitation(
            discord_name=target_discord,
            role=role,
            ign=ign,
            username=username,
            inviter=f"{interaction.user.name} ({interaction.user.mention})",
        )
    except PermissionError as e:
        embed = discord.Embed(
            title="🚨 SICHERHEITSSPERRE: Einladung verweigert",
            description=str(e),
            color=discord.Color.red(),
        )
        embed.set_footer(text="Weihnachtsdorf 7 Security Gateway")
        await interaction.followup.send(embed=embed)
        return
    except Exception as e:
        embed = discord.Embed(
            title="❌ Fehler bei der Einladungserstellung",
            description=f"Die Einladung konnte nicht erstellt werden:\n`{e}`",
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed)
        return

    clean_target = res["discord_name"]
    clean_role = res["role"]
    invite_link = res["invite_link"]
    dm_sent = res["dm_sent"]
    canonical_ign = res.get("minecraft_name")

    embed = discord.Embed(
        title="✉️ Einladung erfolgreich erstellt",
        description=f"Die Einladung für **{clean_target}** (`{res['username']}`) wurde generiert.",
        color=discord.Color.green() if dm_sent else discord.Color.orange(),
    )
    embed.add_field(name="Rolle", value=f"`{clean_role}`", inline=True)
    embed.add_field(name="Erstellt von", value=interaction.user.mention, inline=True)
    if canonical_ign:
        embed.add_field(name="Minecraft IGN", value=f"`{canonical_ign}`", inline=True)
    embed.add_field(
        name="DM-Zustellung",
        value="✅ Erfolgreich per DM zugestellt" if dm_sent else "⚠️ DM fehlgeschlagen (bitte Link manuell weitergeben)",
        inline=False,
    )
    embed.add_field(name="Einladungslink (Backup)", value=f"`{invite_link}`", inline=False)
    embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")

    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Global Interaction Listener for Dynamic Buttons
# ---------------------------------------------------------------------------

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.response.is_done():
        return

    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")

        # Persistent view buttons (handled by views directly)
        if custom_id in (
            "wd_apply_start_btn",
            "wd_appeal_start_btn",
            "wd_manual_invite_start_btn",
            "wd_manual_invite_refresh_list_btn",
        ):
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

    # 4. Spielerliste Channel & Live Participant Board
    try:
        await sync_playerlist_bot(bot, force=True)
        logger.info("Synchronized #spielerliste live participant embed.")
    except Exception as e:
        logger.warning(f"Could not synchronize #spielerliste channel: {e}")

    # 5. Admin Einladungen Channel & Pinned Panel
    invite_ch_id = getattr(settings, "DISCORD_CHANNEL_INVITES_ID", "")
    invite_ch = guild.get_channel(int(invite_ch_id)) if invite_ch_id and invite_ch_id.isdigit() else None
    if not invite_ch:
        for ch in guild.text_channels:
            if any(k in ch.name.lower() for k in ("admin-einladungen", "admin-invites", "einladungen-admin")):
                invite_ch = ch
                break

    if invite_ch:
        try:
            pins = [p async for p in invite_ch.pins()]
            has_panel = any(p.author.id == bot.user.id and "Manuelle Einladungs-Zentrale" in (p.embeds[0].title if p.embeds else "") for p in pins)
            if not has_panel:
                embed = discord.Embed(
                    title="✉️ Weihnachtsdorf 7 • Manuelle Einladungs-Zentrale",
                    description=(
                        "In diesem Kanal können Administratoren manuell neue Teilnehmer und Teammitglieder zum Projekt einladen.\n\n"
                        "📌 **Funktionen:**\n"
                        "• Klicke unten auf **„✉️ Nutzer einladen“**, um das Formular zu öffnen.\n"
                        "• Oder nutze den Befehl `/einladen` bzw. `/invite` direkt hier im Kanal.\n"
                        "• Der Bot generiert automatisch den personalisierten Authentik-Link und stellt die Einladung per DM zu.\n\n"
                        "🛡️ **Sicherheitsrichtlinie:**\n"
                        "Die Rolle `admin` kann aus Sicherheitsgründen **ausschließlich** an Nutzer vergeben werden, die bereits die Rolle **Administrator** auf diesem Discord-Server besitzen."
                    ),
                    color=discord.Color.blue(),
                )
                embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")
                msg = await invite_ch.send(embed=embed, view=ManualInviteStartView())
                try:
                    await msg.pin()
                except Exception:
                    pass
                logger.info(f"Initialized manual invite dashboard in #{invite_ch.name}")
        except Exception as e:
            logger.warning(f"Failed to setup manual invite panel: {e}")


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

    log_admin_event(
        title="🔨 Spieler gesperrt",
        description=f"Spieler `{ign or clean_target}` wurde gesperrt.",
        color=COLOR_ERROR,
        fields=[
            {"name": "Gebannt von", "value": interaction.user.mention, "inline": True},
            {"name": "Grund", "value": clean_reason, "inline": True},
            {"name": "Minecraft Ban", "value": "✅ Ausgeführt" if mc_banned else "⚠️ Fehler/Kein IGN", "inline": True},
            {"name": "BlueMap Ban", "value": "✅ Gesperrt" if bluemap_blocked else "⚠️ Kein Account", "inline": True},
        ],
        author_name=interaction.user.name,
    )

    try:
        await sync_playerlist_bot(bot, force=True)
    except Exception:
        pass


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

    log_admin_event(
        title="✨ Spieler entbannt",
        description=f"Spieler `{ign or clean_target}` wurde erfolgreich entbannt.",
        color=COLOR_SUCCESS,
        fields=[{"name": "Entbannt von", "value": interaction.user.mention, "inline": True}],
        author_name=interaction.user.name,
    )

    try:
        await sync_playerlist_bot(bot, force=True)
    except Exception:
        pass


@tree.command(
    name="einladen",
    description="Lädt einen neuen Spieler oder Administrator manuell zu Weihnachtsdorf 7 ein (Admin).",
)
@app_commands.describe(
    nutzer="Discord-Nutzername, Discord-ID oder @Mention",
    rolle="Rolle: player, mod oder admin (Admin erfordert Discord-Adminrecht)",
    ign="Minecraft Ingame-Name (optional)",
    username="Wunsch-Benutzername (optional)",
)
@app_commands.choices(
    rolle=[
        app_commands.Choice(name="Spieler (Standard)", value="player"),
        app_commands.Choice(name="Moderator", value="mod"),
        app_commands.Choice(name="Administrator", value="admin"),
    ]
)
async def cmd_einladen(
    interaction: discord.Interaction,
    nutzer: str,
    rolle: app_commands.Choice[str],
    ign: Optional[str] = None,
    username: Optional[str] = None,
):
    await handle_manual_invite_submit(
        interaction,
        target_discord=nutzer,
        role=rolle.value,
        ign=ign,
        username=username,
    )


@tree.command(
    name="invite",
    description="Invite a user to Weihnachtsdorf 7 manually (Admin).",
)
@app_commands.describe(
    user="Discord username, Discord ID, or @Mention",
    role="Role: player, mod, or admin (Admin requires Discord Admin role)",
    ign="Minecraft In-Game Name (optional)",
    username="Desired username (optional)",
)
@app_commands.choices(
    role=[
        app_commands.Choice(name="Player", value="player"),
        app_commands.Choice(name="Moderator", value="mod"),
        app_commands.Choice(name="Administrator", value="admin"),
    ]
)
async def cmd_invite(
    interaction: discord.Interaction,
    user: str,
    role: app_commands.Choice[str],
    ign: Optional[str] = None,
    username: Optional[str] = None,
):
    await handle_manual_invite_submit(
        interaction,
        target_discord=user,
        role=role.value,
        ign=ign,
        username=username,
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


@tasks.loop(seconds=30)
async def playerlist_sync_loop():
    """Background task to keep #spielerliste continuously up-to-date."""
    try:
        await sync_playerlist_bot(bot)
    except Exception as e:
        logger.debug(f"Error in playerlist_sync_loop: {e}")


@playerlist_sync_loop.before_loop
async def before_playerlist_sync_loop():
    await bot.wait_until_ready()


@bot.event
async def on_member_remove(member: discord.Member):
    """
    Triggered when a user leaves the Discord server (or is kicked/banned).
    Preserves the user's account in LDAP/Authentik, but revokes all project rights,
    groups, whitelist, and SSO access. Logs the event to #admin-logs and refreshes #spielerliste.
    """
    if settings.DISCORD_GUILD_ID and member.guild.id != int(settings.DISCORD_GUILD_ID):
        return

    if member.bot:
        return

    logger.info(f"Member left guild {member.guild.name}: {member.name} (ID: {member.id}, Display: {member.display_name})")

    try:
        # Search by ID first
        res = await asyncio.to_thread(
            revoke_user_privileges,
            target=str(member.id),
            reason="Discord-Server verlassen",
            initiator="Discord Gateway (Member Remove)",
        )

        # Fallback to member name
        if not res.get("found"):
            res = await asyncio.to_thread(
                revoke_user_privileges,
                target=member.name,
                reason="Discord-Server verlassen",
                initiator="Discord Gateway (Member Remove)",
            )

        # Fallback to display name if different
        if not res.get("found") and member.display_name != member.name:
            res = await asyncio.to_thread(
                revoke_user_privileges,
                target=member.display_name,
                reason="Discord-Server verlassen",
                initiator="Discord Gateway (Member Remove)",
            )

        if not res.get("found"):
            logger.info(f"Member {member.name} ({member.id}) had no registered project account.")
            log_admin_event(
                title="🚪 Discord-Server verlassen",
                description=f"**{member.name}** (`{member.id}`) hat den Server verlassen.\n*(Kein registrierter Projekt-Account verknüpft)*",
                color=COLOR_INFO,
                fields=[
                    {"name": "Discord", "value": f"{member.name} (`{member.id}`)", "inline": True},
                    {"name": "Status", "value": "Kein Account verknüpft", "inline": True},
                ],
                author_name="Discord Gateway",
            )
        else:
            logger.info(
                f"Successfully revoked privileges for departed member {member.name} "
                f"(Username: {res.get('username')}). Account preserved in deactivated state."
            )
            await sync_playerlist_bot(bot, force=True)

    except Exception as e:
        logger.error(f"Error handling member remove for {member.name} ({member.id}): {e}", exc_info=True)
        try:
            log_admin_event(
                title="🚨 Fehler bei Server-Verlassen Behandlung",
                description=f"Beim Entzug der Rechte für **{member.name}** (`{member.id}`) ist ein Fehler aufgetreten.",
                color=COLOR_ERROR,
                fields=[
                    {"name": "Fehler", "value": f"`{str(e)[:1000]}`", "inline": False},
                ],
                author_name="Discord Gateway",
            )
        except Exception:
            pass


@bot.event
async def on_ready():
    logger.info(f"Discord Bot logged in as {bot.user} (ID: {bot.user.id})")

    # Register persistent views so buttons work across restarts
    bot.add_view(ApplyStartView())
    bot.add_view(AppealStartView())
    bot.add_view(ManualInviteStartView())

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

    # Start automatic playerlist update loop (30s interval)
    try:
        if not playerlist_sync_loop.is_running():
            playerlist_sync_loop.start()
            logger.info("Started playerlist automatic synchronization loop (30s interval).")
    except Exception as e:
        logger.error(f"Failed to start playerlist_sync_loop: {e}")


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
