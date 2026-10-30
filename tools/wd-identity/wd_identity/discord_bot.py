"""
Discord bot for Weihnachtsdorf 7.
Features:
- Slash commands: /auth-code, /code, /passwort-reset, /status, /links, /anmelden, /entbannung, /ban-spieler, /pardon-spieler
- Interactive candidate applications (#anmeldung) and unban appeals (#entbannung)
- Admin review dashboard (#admin-anmeldungen) with Accept / Reject buttons & cooldowns
- BlueMap & Minecraft ban integration
- Direct message dispatcher for invitations and recovery links
- Welcome guide DM for newly onboarded team members & players
"""

import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
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
from wd_identity.membership import (
    purge_user_completely,
    restore_user_privileges,
    revoke_user_privileges,
)
from wd_identity.planka_sync import PlankaSyncClient
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

🔒 **Zwei-Faktor-Authentifizierung (2FA):**
• Du kannst bei jedem Login frei wählen:
  1. **Discord-Bot Einmalcode (Empfohlen):** Nutze `/auth-code` (oder `/code`) hier im Discord, um dir sekundenschnell einen 6-stelligen Login-Code generieren zu lassen.
  2. **Authenticator-App (TOTP):** Falls eingerichtet, kannst du alternativ jederzeit den Code deiner Authenticator-App (z. B. Google Authenticator, 2FAS) eingeben.
• Bei Passwort-Verlust kannst du jederzeit `/passwort-reset` nutzen.

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


class BroadcastModal(discord.ui.Modal, title="📢 Admin-Rundruf senden"):
    """Modal for sending broadcasts / DMs to project roles or individual users."""
    target_input = discord.ui.TextInput(
        label="Empfänger (all/player/staff/admin/User)",
        placeholder="all, player, staff, admin ODER @Nutzer / Username",
        default="all",
        required=True,
        max_length=100,
        style=discord.TextStyle.short,
    )
    message_input = discord.ui.TextInput(
        label="Nachricht",
        placeholder="Inhalt der Ankündigung oder Benachrichtigung...",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        role_or_user = self.target_input.value.strip().lower()
        clean_msg = self.message_input.value.strip()

        ldap = LDAPClient()
        recipients = []
        target_label = role_or_user

        if role_or_user in ("all", "player", "staff", "admin", "mod"):
            admins, mods = ldap.get_admins_and_mods()
            players = ldap.get_players()
            if role_or_user == "admin":
                ul = admins
            elif role_or_user == "mod":
                ul = mods
            elif role_or_user == "staff":
                ul = admins + mods
            elif role_or_user == "player":
                ul = players
            else:
                ul = admins + mods + players
            for r in ul:
                h = r.get("discordId") or r.get("discordName") or r.get("uid")
                if h and h not in recipients:
                    recipients.append(h)
        else:
            clean_u = role_or_user.replace("<@", "").replace(">", "").replace("!", "")
            u = ldap.search_by_discord(clean_u) or ldap.get_user(clean_u)
            recipients.append(clean_u if not u else (u.get("discordId") or u.get("discordName") or u["uid"]))

        if not recipients:
            await interaction.followup.send(f"⚠️ Keine Empfänger für '{role_or_user}' gefunden.", ephemeral=True)
            return

        dm_text = (
            f"📢 **Wichtige Mitteilung • Weihnachtsdorf 7** 🎄\n\n"
            f"{clean_msg}\n\n"
            f"*Gesendet von Administrator {interaction.user.name} via Admin-Steuerung.*"
        )

        succ = 0
        fail = 0
        for rec in recipients:
            ok = await asyncio.to_thread(send_dm, rec, dm_text)
            if ok:
                succ += 1
            else:
                fail += 1

        log_admin_event(
            title="📢 Rundruf versendet",
            description=f"Administrator {interaction.user.mention} hat einen Rundruf an **{target_label}** versendet.",
            color=COLOR_SUCCESS if fail == 0 else COLOR_WARNING,
            fields=[
                {"name": "Ziel", "value": target_label, "inline": True},
                {"name": "Status", "value": f"✅ {succ} zugestellt / ⚠️ {fail} fehlgeschlagen", "inline": True},
                {"name": "Nachricht", "value": clean_msg[:400], "inline": False},
            ],
            author_name=interaction.user.name,
        )

        await interaction.followup.send(
            f"📢 **Rundruf abgeschlossen!**\n"
            f"• Zugestellt: ✅ **{succ}**\n"
            f"• Fehlgeschlagen (DMs geschlossen): ⚠️ **{fail}**\n"
            f"• Zielgruppe: `{target_label}`",
            ephemeral=True,
        )


class AdminControlDashboardView(discord.ui.View):
    """Interactive control dashboard pinned in #admin-steuerung."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Projekt starten",
        style=discord.ButtonStyle.success,
        emoji="🚀",
        custom_id="wd_ctrl_launch_btn",
    )
    async def btn_launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        mc = MinecraftSyncClient()
        await asyncio.to_thread(mc.launch_project)
        log_admin_event(
            title="🎉 Projekt gestartet (via Dashboard)",
            description=f"Administrator {interaction.user.mention} hat das Projekt gestartet.",
            color=COLOR_SUCCESS,
            author_name=interaction.user.name,
        )
        await sync_playerlist_bot(bot, force=True)
        await interaction.followup.send("🚀 **Weihnachtsdorf 7 wurde gestartet!** Whitelist für alle Spieler geöffnet.", ephemeral=True)

    @discord.ui.button(
        label="Projekt sperren",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="wd_ctrl_lock_btn",
    )
    async def btn_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        mc = MinecraftSyncClient()
        await asyncio.to_thread(mc.lock_project)
        log_admin_event(
            title="🔒 Projekt gesperrt (via Dashboard)",
            description=f"Administrator {interaction.user.mention} hat das Projekt in den Wartungsmodus versetzt.",
            color=COLOR_WARNING,
            author_name=interaction.user.name,
        )
        await sync_playerlist_bot(bot, force=True)
        await interaction.followup.send("🔒 **Projekt gesperrt!** Server ist im Wartungsmodus (nur Team).", ephemeral=True)

    @discord.ui.button(
        label="Sync forstieren",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="wd_ctrl_sync_btn",
    )
    async def btn_sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        mc = MinecraftSyncClient()
        authentik = AuthentikClient()
        await asyncio.to_thread(mc.sync_all, True)
        await asyncio.to_thread(authentik.trigger_ldap_sync)
        await sync_playerlist_bot(bot, force=True)
        await interaction.followup.send("🔄 **Komplett-Synchronisierung erfolgreich abgeschlossen!**", ephemeral=True)

    @discord.ui.button(
        label="Nutzer einladen",
        style=discord.ButtonStyle.success,
        emoji="✉️",
        custom_id="wd_ctrl_invite_btn",
    )
    async def btn_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können Nutzer einladen.", ephemeral=True)
            return
        await interaction.response.send_modal(ManualInviteModal())

    @discord.ui.button(
        label="Rundruf / Broadcast",
        style=discord.ButtonStyle.secondary,
        emoji="📢",
        custom_id="wd_ctrl_broadcast_btn",
    )
    async def btn_broadcast(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können Rundrufe senden.", ephemeral=True)
            return
        await interaction.response.send_modal(BroadcastModal())


# ---------------------------------------------------------------------------
# Maintenance Countdown State & Automation
# ---------------------------------------------------------------------------

active_maintenance: Optional[Dict[str, Any]] = None


class MaintenanceAbortView(discord.ui.View):
    """View attached to active maintenance countdown embed with abort button."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Wartungs-Countdown abbrechen",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
        custom_id="wd_maint_abort_btn",
    )
    async def btn_abort(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können den Countdown abbrechen.", ephemeral=True)
            return
        await cancel_maintenance_countdown(interaction)


async def cancel_maintenance_countdown(interaction: discord.Interaction):
    global active_maintenance
    if not active_maintenance:
        if interaction.response.is_done():
            await interaction.followup.send("ℹ️ Aktuell läuft kein Wartungs-Countdown.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Aktuell läuft kein Wartungs-Countdown.", ephemeral=True)
        return

    task = active_maintenance.get("task")
    msg = active_maintenance.get("message")
    active_maintenance = None

    if task and not task.done():
        task.cancel()

    mc = MinecraftSyncClient()
    await asyncio.to_thread(
        mc.broadcast_ingame,
        message="Die geplante Serverwartung wurde abgebrochen. Normaler Spielbetrieb!",
        title="Wartung abgebrochen",
        subtitle="Viel Spaß weiterhin!",
    )

    log_admin_event(
        title="🛑 Wartungs-Countdown abgebrochen",
        description=f"Administrator {interaction.user.mention} hat den laufenden Wartungs-Countdown abgebrochen.",
        color=COLOR_SUCCESS,
        author_name=interaction.user.name,
    )

    if msg:
        try:
            embed = discord.Embed(
                title="🛑 Serverwartung abgebrochen",
                description=f"Der Countdown wurde von {interaction.user.mention} vorzeitig gestoppt.\nDer Server bleibt normal geöffnet.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"Abgebrochen von {interaction.user.name}")
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass

    if interaction.response.is_done():
        await interaction.followup.send("✅ Der Wartungs-Countdown wurde erfolgreich abgebrochen.", ephemeral=True)
    else:
        await interaction.response.send_message("✅ Der Wartungs-Countdown wurde erfolgreich abgebrochen.", ephemeral=True)


async def run_maintenance_countdown(duration_minutes: int, reason: str, initiator: discord.Member, message: discord.Message):
    global active_maintenance
    mc = MinecraftSyncClient()
    total_seconds = duration_minutes * 60
    end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)

    # Initial announcement
    await asyncio.to_thread(
        mc.broadcast_ingame,
        message=f"Serverwartung in {duration_minutes} Minuten! Grund: {reason}",
        title=f"Wartung in {duration_minutes} Min",
        subtitle=reason,
    )

    milestones = {
        1800: "30 Minuten",
        900: "15 Minuten",
        600: "10 Minuten",
        300: "5 Minuten",
        180: "3 Minuten",
        120: "2 Minuten",
        60: "1 Minute",
        30: "30 Sekunden",
        10: "10 Sekunden",
        5: "5 Sekunden",
    }

    try:
        while True:
            remaining = int((end_time - datetime.now(timezone.utc)).total_seconds())
            if remaining <= 0:
                break

            if remaining in milestones:
                label = milestones[remaining]
                await asyncio.to_thread(
                    mc.broadcast_ingame,
                    message=f"Achtung: Serverwartung in {label}! Bitte bringt euch in Sicherheit.",
                    title=f"Wartung in {label}",
                    subtitle="Bitte ausloggen!",
                )

            # Update Discord embed
            if remaining % 30 == 0 or remaining in (60, 30, 10):
                try:
                    mins = remaining // 60
                    secs = remaining % 60
                    embed = discord.Embed(
                        title="⏳ Geplante Serverwartung • Countdown läuft",
                        description=(
                            f"**Verbleibende Zeit:** `{mins:02d}:{secs:02d}` Minuten\n"
                            f"**Grund:** {reason}\n"
                            f"**Geplant von:** {initiator.mention}\n\n"
                            f"⚠️ *Bei Ablauf werden alle Spieler gekickt und der Server in den Wartungsmodus versetzt.*"
                        ),
                        color=discord.Color.red() if remaining <= 60 else discord.Color.orange(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.set_footer(text="Klicke auf den Button unten, um den Countdown vorzeitig abzubrechen.")
                    await message.edit(embed=embed, view=MaintenanceAbortView())
                except Exception:
                    pass

            await asyncio.sleep(1)

        # Countdown finished!
        active_maintenance = None

        # 1. Final Ingame Kick & Lock
        kick_reason = f"Geplante Serverwartung: {reason}"
        await asyncio.to_thread(
            mc.execute_commands,
            [f'kick @a "{kick_reason}"']
        )
        await asyncio.to_thread(mc.lock_project)

        # 2. Audit log
        log_admin_event(
            title="🔒 Serverwartung aktiviert (Countdown abgelaufen)",
            description=f"Der Countdown von {initiator.mention} ist abgelaufen. Server wurde gesperrt und alle Spieler gekickt.",
            color=COLOR_WARNING,
            fields=[
                {"name": "Dauer", "value": f"{duration_minutes} Minuten", "inline": True},
                {"name": "Grund", "value": reason, "inline": True},
                {"name": "Status", "value": "🔒 Wartungsmodus aktiv", "inline": True},
            ],
            author_name=initiator.name,
        )

        # 3. Update playerlist & Discord message
        await sync_playerlist_bot(bot, force=True)

        embed = discord.Embed(
            title="🔒 Serverwartung aktiv (Gesperrt)",
            description=(
                f"**Der Countdown ist abgelaufen.**\n\n"
                f"• Alle Spieler wurden mit Grund `{reason}` gekickt.\n"
                f"• Die Server-Whitelist wurde geschlossen (nur Teammitglieder).\n"
                f"• BlueMap befindet sich im Wartungsmodus."
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Wartung ausgeführt von {initiator.name}")
        await message.edit(embed=embed, view=None)

    except asyncio.CancelledError:
        logger.info("Maintenance countdown task was cancelled.")
        raise


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


class AuthCodeButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Login-Code anfordern",
        style=discord.ButtonStyle.primary,
        emoji="🔑",
        custom_id="wd_auth_code_btn",
    )
    async def btn_auth_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_auth_code_request(interaction)


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


class ReclaimAccountView(discord.ui.View):
    """View sent to #admin-logs when a departed member rejoins the Discord server."""
    def __init__(self, target_uid: str, discord_id: str, ign: str):
        super().__init__(timeout=None)
        self.target_uid = target_uid
        self.discord_id = discord_id
        self.ign = ign
        self.add_item(
            discord.ui.Button(
                label="Account reaktivieren",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"reclaim:accept:{target_uid}:{discord_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Inaktiv lassen",
                style=discord.ButtonStyle.secondary,
                emoji="✖️",
                custom_id=f"reclaim:dismiss:{target_uid}:{discord_id}",
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


class OptOutModal(discord.ui.Modal, title="Vom Weihnachtsdorf 7 abmelden"):
    """Modal presented to players who want to deregister from the project."""
    reason_input = discord.ui.TextInput(
        label="Grund für deine Abmeldung (optional)",
        placeholder="z. B. Zeitmangel, Schul-/Unistress, Pause...",
        required=False,
        max_length=200,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_reason = self.reason_input.value.strip() or "Keine Begründung angegeben"
        discord_id = str(interaction.user.id)

        ldap = LDAPClient()
        user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(interaction.user.name)
        status = applications.get_user_status(discord_id)

        if not user and not status.get("has_pending"):
            await interaction.followup.send(
                "❌ Du bist aktuell nicht als Teilnehmer registriert und hast keine offene Bewerbung.",
                ephemeral=True,
            )
            return

        res = await asyncio.to_thread(
            revoke_user_privileges,
            target=user if user else discord_id,
            reason=f"Selbstabmeldung: {user_reason}",
            initiator=f"{interaction.user.name} (Selbstabmeldung)",
        )

        ign = res.get("minecraft_name")
        uid = res.get("username") or interaction.user.name

        try:
            log_admin_event(
                title="👋 Spieler hat sich selbst abgemeldet",
                description=f"**{interaction.user.name}** ({interaction.user.mention}) hat sich selbstständig vom Weihnachtsdorf 7 abgemeldet.",
                color=COLOR_INFO,
                fields=[
                    {"name": "Discord-Nutzer", "value": f"{interaction.user.name} (`{interaction.user.id}`)", "inline": True},
                    {"name": "Account / IGN", "value": f"`{uid}` (IGN: `{ign}`)" if ign else f"`{uid}`", "inline": True},
                    {"name": "Grund", "value": user_reason, "inline": False},
                    {"name": "Status", "value": "🔒 Berechtigungen & Whitelist entzogen (Account bleibt erhalten)", "inline": False},
                ],
                author_name="Selbstabmeldung",
            )
        except Exception as e:
            logger.error(f"Error logging self-optout event: {e}")

        await sync_playerlist_bot(bot, force=True)

        await interaction.followup.send(
            "✅ **Du hast dich erfolgreich vom Weihnachtsdorf 7 abgemeldet.**\n\n"
            "Deine Whitelist und deine Rollen wurden vorübergehend deaktiviert.\n"
            "Dein Account und deine bisherigen Daten bleiben sicher im System gespeichert.\n"
            "Wenn du später wieder mitmachen möchtest, kannst du dich jederzeit wieder bei den Admins melden. Danke für deine Teilnahme! 🎄",
            ephemeral=True,
        )


async def trigger_optout_flow(interaction: discord.Interaction):
    """Validates user eligibility and displays the opt-out modal."""
    if interaction.response.is_done():
        return

    discord_id = str(interaction.user.id)
    ldap = LDAPClient()
    user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(interaction.user.name)
    status = applications.get_user_status(discord_id)

    if not user and not status.get("has_pending"):
        await interaction.response.send_message(
            "❌ **Du bist aktuell nicht für Weihnachtsdorf 7 registriert und hast keine offene Bewerbung.**\n"
            "Eine Abmeldung ist daher nicht notwendig.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(OptOutModal())


class ConfirmPurgeView(discord.ui.View):
    def __init__(self, target: str, display_name: str, initiator: str, reason: str):
        super().__init__(timeout=60)
        self.target = target
        self.display_name = display_name
        self.initiator = initiator
        self.reason = reason

    @discord.ui.button(label="Endgültig löschen", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass

        res = await asyncio.to_thread(
            purge_user_completely,
            target=self.target,
            reason=self.reason,
            initiator=self.initiator,
        )

        if not res.get("success"):
            err_msg = res.get("error", "Unbekannter Fehler beim Löschen.")
            await interaction.followup.send(f"❌ **Fehler beim Löschen:** {err_msg}", ephemeral=True)
            return

        deleted_user = res.get("username") or self.display_name
        await interaction.followup.send(
            f"✅ **Account erfolgreich gelöscht!**\n\n"
            f"Der Account `{deleted_user}` wurde spurlos und vollständig aus allen Systemen (OpenLDAP, Authentik, Planka, Minecraft & Datenbank) entfernt.\n"
            f"Du kannst dich jetzt jederzeit mit `/anmelden` wieder bewerben.",
            ephemeral=True,
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(
                content="❌ **Löschvorgang abgebrochen.** Es wurden keine Daten gelöscht.",
                view=self,
            )
        except Exception:
            pass


async def trigger_purge_flow(
    interaction: discord.Interaction,
    ziel: Optional[str] = None,
    grund: Optional[str] = None,
):
    """Initiates the account purge flow with confirmation."""
    clean_target = (ziel or "").strip().replace("<@", "").replace(">", "").replace("!", "")
    caller_is_admin = is_admin_user(interaction.user)

    if clean_target and clean_target != str(interaction.user.id) and clean_target.lower() != interaction.user.name.lower():
        if not caller_is_admin:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ **Keine Berechtigung:** Du kannst nur deinen eigenen Account löschen. Lasse das Feld `ziel` leer.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ **Keine Berechtigung:** Du kannst nur deinen eigenen Account löschen. Lasse das Feld `ziel` leer.",
                    ephemeral=True,
                )
            return
        target_to_purge = clean_target
        initiator_str = f"{interaction.user.name} (Admin Purge)"
        reason_str = grund.strip() if grund else "Admin-Löschung via Discord"
    else:
        target_to_purge = str(interaction.user.id)
        initiator_str = f"{interaction.user.name} (Selbstlöschung)"
        reason_str = grund.strip() if grund else "Selbstlöschung via Discord"

    ldap = LDAPClient()
    user_entry = None
    if target_to_purge.isdigit():
        user_entry = ldap.search_by_discord(target_to_purge)
    if not user_entry:
        user_entry = (
            ldap.get_user(target_to_purge)
            or ldap.search_by_discord(target_to_purge)
            or ldap.search_by_minecraft(target_to_purge)
        )

    status = applications.get_user_status(target_to_purge) if target_to_purge.isdigit() else {}

    if not user_entry and not status.get("has_pending") and not status.get("is_banned") and not status.get("is_rejected"):
        msg = (
            "❌ **Kein Account gefunden:** Unter deinen Daten ist kein aktiver Weihnachtsdorf-Account registriert."
            if target_to_purge == str(interaction.user.id)
            else f"❌ **Kein Account gefunden für:** `{target_to_purge}`."
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        return

    display_name = user_entry.get("uid") if user_entry else target_to_purge

    # Protection check before prompt
    if display_name.lower() in ("tigimax", "akadmin"):
        msg = f"🛡️ **Geschützter Account:** Der Account `{display_name}` ist ein interner System-Account und kann nicht gelöscht werden."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        return

    view = ConfirmPurgeView(
        target=target_to_purge,
        display_name=display_name,
        initiator=initiator_str,
        reason=reason_str,
    )

    prompt_text = (
        f"⚠️ **Möchtest du den Account `{display_name}` wirklich unwiderruflich und spurlos löschen?**\n\n"
        f"Folgende Daten werden permanent gelöscht:\n"
        f"• **OpenLDAP**: Account & Gruppenmitgliedschaften\n"
        f"• **Authentik**: IdP-Konto & Einladungs-Token\n"
        f"• **Planka**: Profil & Projekt-Mitgliedschaften\n"
        f"• **Minecraft**: Whitelist-Eintrag & LuckPerms-Rollen\n"
        f"• **Datenbank**: Bewerbungshistorie & eventuelle Sperren\n\n"
        f"Danach kann sich der Nutzer über `/anmelden` wieder komplett frisch registrieren."
    )

    if not interaction.response.is_done():
        await interaction.response.send_message(prompt_text, view=view, ephemeral=True)
    else:
        await interaction.followup.send(prompt_text, view=view, ephemeral=True)


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


async def handle_auth_code_request(interaction: discord.Interaction):
    """Generates a 6-digit one-time login code for Authentik (strictly regular players only)."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    ldap = LDAPClient()
    authentik = AuthentikClient()

    discord_id = str(interaction.user.id)
    discord_tag = interaction.user.name

    user = ldap.search_by_discord(discord_id) or ldap.search_by_discord(discord_tag)
    if not user:
        await interaction.followup.send(
            "❌ **Kein Account verknüpft**\n\n"
            "Dein Discord-Account ist mit keinem registrierten Weihnachtsdorf-Account verknüpft.\n"
            "Falls du dich für Weihnachtsdorf 7 bewerben möchtest, klicke auf **„Jetzt bewerben“** oder nutze den Befehl `/anmelden`.",
            ephemeral=True,
        )
        return

    # Check ban status
    if user.get("accountStatus") == "banned":
        await interaction.followup.send(
            "❌ **Account gesperrt**\n\n"
            "Dein Account ist derzeit gesperrt. Du kannst einen Entbannungsantrag mit `/entbannung` stellen.",
            ephemeral=True,
        )
        return

    # Strict role security check: STRICTLY PROHIBITED FOR STAFF (admin / mod)
    role = (user.get("role") or "player").lower().strip()
    if role in ("admin", "mod"):
        await interaction.followup.send(
            "⛔ **Sicherheitshinweis für Teammitglieder**\n\n"
            "Aus Sicherheitsgründen ist die Authentifizierung per Discord-Einmalcode für Administratoren und Moderatoren **deaktiviert**.\n\n"
            "Für Team-Accounts ist die Nutzung einer **Authenticator-App (TOTP)** (z. B. 2FAS, Google Authenticator, Aegis) verpflichtend.\n"
            "Bitte nutze den 6-stelligen Code aus deiner Authenticator-App für die Anmeldung.\n\n"
            "Falls du dein Gerät verloren hast, wende dich bitte direkt an die Projektleitung.",
            ephemeral=True,
        )
        return

    # Generate 6-digit static MFA token for regular player
    username = user["uid"]
    token = authentik.generate_static_token_for_user(username)
    if not token:
        await interaction.followup.send(
            f"⚠️ Es konnte kein Login-Code für `{username}` generiert werden. Bitte versuche es in wenigen Momenten erneut oder kontaktiere das Team.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🔑 Dein einmaliger Login-Code",
        description=(
            f"Gib diesen 6-stelligen Code bei der Authentik-Anmeldung im Feld **Authenticator-Code** ein:\n\n"
            f"# `{token}`\n\n"
            f"⏱️ **Gültigkeit:** Einmalig für deinen nächsten Login (empfohlen innerhalb von 5 Minuten).\n"
            f"👤 **Account:** `{username}`\n"
            f"🌐 **Login-Portal:** {settings.AUTHENTIK_BASE_URL}"
        ),
        color=discord.Color.from_rgb(46, 204, 113),
    )
    embed.set_footer(text="Weihnachtsdorf 7 Security • Niemals an Dritte weitergeben")
    await interaction.followup.send(embed=embed, ephemeral=True)


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
        "attributes": {
            "discord_id": discord_id,
            "discord_name": discord_name,
            "role": "player",
        },
        "attributes.discord_id": discord_id,
        "attributes.discord_name": discord_name,
        "attributes.role": "player",
    }
    invite_link = None
    try:
        clean_user = "".join(c for c in discord_name.lower() if c.isalnum() or c in ("-", "_")) or f"user{discord_id[-4:]}"
        # Protection: Never overwrite master internal admin account 'tigimax' or 'akadmin'
        if clean_user.lower() in ("tigimax", "akadmin"):
            clean_user = "tigimax_player"
        fixed_data["username"] = clean_user
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

    dm_sent = False
    invite_link = None
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
        dm_sent = send_dm_rest(discord_id, dm_msg)
    else:
        # User was rejected initially and had no account yet: Send Authentik invite link
        clean_user = "".join(c for c in discord_name.lower() if c.isalnum() or c in ("-", "_")) or f"user{discord_id[-4:]}"
        # Protection: Never overwrite master internal admin account 'tigimax' or 'akadmin'
        if clean_user.lower() in ("tigimax", "akadmin"):
            clean_user = "tigimax_player"
        authentik.delete_user(clean_user)
        fixed_data = {
            "discord_id": discord_id,
            "discord_name": discord_name,
            "role": "player",
            "username": clean_user,
            "attributes": {
                "discord_id": discord_id,
                "discord_name": discord_name,
                "role": "player",
            },
            "attributes.discord_id": discord_id,
            "attributes.discord_name": discord_name,
            "attributes.role": "player",
        }
        invite_link = authentik.create_invitation(
            name=f"invite-{clean_user}",
            fixed_data=fixed_data,
        )
        dm_msg = (
            f"🎉 **Dein Einspruch für Weihnachtsdorf 7 wurde angenommen!** 🎄\n\n"
            f"Du kannst dich ab sofort als **Spieler** registrieren:\n"
            f"👉 {invite_link}\n\n"
            f"*Dieser Link verfällt nach 24 Stunden.*"
        )
        dm_sent = send_dm_rest(discord_id, dm_msg)

    applications.accept_application(app_id, str(interaction.user.id), interaction.user.name)

    if interaction.message:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        new_embed = interaction.message.embeds[0]
        new_embed.color = discord.Color.green()
        if was_previously_enrolled:
            detail_msg = f"Minecraft & BlueMap freigeschaltet ({user.get('minecraftName')})"
        else:
            detail_msg = "📨 Einladungslink per DM versendet" if dm_sent else "⚠️ DM fehlgeschlagen (bitte Link manuell übermitteln)"
        new_embed.add_field(
            name="Status",
            value=f"✅ **Entbannt / Angenommen** von {interaction.user.mention} (<t:{now_ts}:R>)\n{detail_msg}",
            inline=False,
        )
        if not was_previously_enrolled and not dm_sent and invite_link:
            new_embed.add_field(name="Einladungslink (Backup)", value=f"`{invite_link}`", inline=False)
        await interaction.message.edit(embed=new_embed, view=None)

    msg = f"✅ Entbannungsantrag #{app_id} angenommen!"
    if not was_previously_enrolled and invite_link:
        msg += f" Einladungslink {'per DM versendet.' if dm_sent else f': {invite_link}'}"
    await interaction.followup.send(msg, ephemeral=True)

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


async def handle_reclaim_accept(interaction: discord.Interaction, target_uid: str, discord_id: str):
    """Admin clicks [Account reaktivieren] for a returned user."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren dürfen Accounts reaktivieren.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        # Verify if returning user has Discord Admin permissions
        is_discord_admin = False
        if discord_id and interaction.guild:
            member = interaction.guild.get_member(int(discord_id))
            if member:
                is_discord_admin = is_admin_user(member)

        target_role = "admin" if is_discord_admin else "player"

        res = await asyncio.to_thread(
            restore_user_privileges,
            target=target_uid,
            role=target_role,
            initiator=f"{interaction.user.name} ({interaction.user.id})",
        )

        disabled_view = discord.ui.View()
        if interaction.message and interaction.message.components:
            for row in interaction.message.components:
                for child in row.children:
                    btn = discord.ui.Button(
                        label=child.label,
                        style=child.style,
                        emoji=child.emoji,
                        disabled=True,
                    )
                    disabled_view.add_item(btn)

        embed = interaction.message.embeds[0] if (interaction.message and interaction.message.embeds) else discord.Embed(title="Account reaktiviert")
        embed.color = discord.Color.green()
        embed.add_field(
            name="✅ Reaktiviert von",
            value=f"{interaction.user.mention} (Rolle: `{target_role}`)",
            inline=False,
        )
        embed.set_footer(text=f"Reaktiviert von {interaction.user.name} • {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')}")
        await interaction.edit_original_response(embed=embed, view=disabled_view)

        if discord_id:
            ign = res.get("minecraft_name") or target_uid
            dm_msg = (
                f"🎄 **Willkommen zurück im Weihnachtsdorf 7!** 🎄\n\n"
                f"Ein Administrator ({interaction.user.name}) hat deinen Account erfolgreich **reaktiviert**!\n\n"
                f"🎮 **Status:**\n"
                f"• Rolle: `{target_role.upper()}`\n"
                f"• Whitelist & Rechte auf dem Minecraft-Server (`tigimax.mooo.com`) sind wieder aktiv.\n"
                f"• Deine Portale & SSO-Logins sind wieder freigeschaltet.\n\n"
                f"Viel Spaß wieder auf dem Server! 🎅"
            )
            send_dm(discord_id, dm_msg)

        await sync_playerlist_bot(bot, force=True)

    except Exception as e:
        logger.error(f"Error reclaiming account for {target_uid}: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Fehler bei der Reaktivierung: {e}", ephemeral=True)


async def handle_reclaim_dismiss(interaction: discord.Interaction, target_uid: str, discord_id: str):
    """Admin clicks [Inaktiv lassen] for a returned user."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
        return

    disabled_view = discord.ui.View()
    if interaction.message and interaction.message.components:
        for row in interaction.message.components:
            for child in row.children:
                btn = discord.ui.Button(
                    label=child.label,
                    style=child.style,
                    emoji=child.emoji,
                    disabled=True,
                )
                disabled_view.add_item(btn)

    embed = interaction.message.embeds[0] if (interaction.message and interaction.message.embeds) else discord.Embed(title="Account nicht reaktiviert")
    embed.color = discord.Color.dark_grey()
    embed.add_field(
        name="✖️ Inaktiv belassen",
        value=f"Von {interaction.user.mention} als inaktiv belassen.",
        inline=False,
    )
    embed.set_footer(text=f"Inaktiv belassen von {interaction.user.name} • {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')}")
    await interaction.response.edit_message(embed=embed, view=disabled_view)


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
            "wd_optout_start_btn",
            "wd_appeal_start_btn",
            "wd_manual_invite_start_btn",
            "wd_manual_invite_refresh_list_btn",
            "wd_ctrl_launch_btn",
            "wd_ctrl_lock_btn",
            "wd_ctrl_sync_btn",
            "wd_ctrl_invite_btn",
            "wd_ctrl_broadcast_btn",
            "wd_maint_abort_btn",
        ):
            return

        if custom_id.startswith("reclaim:accept:"):
            parts = custom_id.split(":")
            target_uid = parts[2]
            discord_id = parts[3] if len(parts) > 3 else ""
            await handle_reclaim_accept(interaction, target_uid, discord_id)
            return

        if custom_id.startswith("reclaim:dismiss:"):
            parts = custom_id.split(":")
            target_uid = parts[2]
            discord_id = parts[3] if len(parts) > 3 else ""
            await handle_reclaim_dismiss(interaction, target_uid, discord_id)
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
    candidates = ["admin-anträge", "admin-antraege", "admin-anmeldungen", "anmeldungen-admin", "anträge-admin", "mod-chat"]
    for name in candidates:
        for ch in guild.text_channels:
            if name in ch.name.lower():
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
        if any(k in ch.name.lower() for k in ("anmeldung", "anmeldungen")) and (not category or ch.category_id == category.id):
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
        embed = discord.Embed(
            title="🎄 Anmeldung für Weihnachtsdorf 7 🎄",
            description=(
                "Willkommen im weihnachtlichen SMP-Projekt!\n\n"
                "📝 **So funktioniert die Bewerbung:**\n"
                "1. Klicke unten auf den Button **„Jetzt bewerben“** oder nutze den Befehl `/anmelden` hier im Channel.\n"
                "2. Gib eine kurze Nachricht an das Team ein (wer du bist, warum du mitmachen möchtest).\n"
                "3. Dein Antrag wird vertraulich an die Administratoren übermittelt.\n"
                "4. Nach der Annahme erhältst du eine Direktnachricht (DM) mit deinem persönlichen Registrierungs-Link.\n\n"
                "🔐 **Login im Webportal:**\n"
                "Sobald du registriert bist, kannst du dich im Webportal anmelden. Wenn du nach dem 2FA-Code gefragt wirst, sendet dir dieser Bot den 6-stelligen Login-Code automatisch per Direktnachricht (DM) zu!"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")

        guide_msg = None
        pins = [p async for p in anmeldung_ch.pins()]
        for p in pins:
            if p.author.id == bot.user.id and "Anmeldung für Weihnachtsdorf 7" in (p.embeds[0].title if p.embeds else ""):
                guide_msg = p
                break

        if guide_msg:
            try:
                await guide_msg.edit(embed=embed, view=ApplyStartView())
            except Exception as e:
                logger.warning(f"Failed to update pinned guide in #anmeldung: {e}")
        else:
            msg = await anmeldung_ch.send(embed=embed, view=ApplyStartView())
            try:
                await msg.pin()
            except Exception:
                pass

    # 2. Entbannung Channel
    entbannung_ch = None
    for ch in guild.text_channels:
        if any(k in ch.name.lower() for k in ("entbannung", "entbannungsanträge", "entbannungsantrag")) and (not category or ch.category_id == category.id):
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
                name="admin-anträge",
                category=category,
                overwrites=overwrites,
                topic="Prüfung eingehender Aufnahme- und Entbannungsanträge.",
            )
            logger.info(f"Created admin channel #{admin_ch.name} (ID: {admin_ch.id})")
        except Exception as e:
            logger.warning(f"Could not create #admin-anträge channel: {e}")

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

    # 6. Admin Steuerung Channel & Pinned Management Dashboard
    steuerung_ch_id = getattr(settings, "DISCORD_CHANNEL_STEURUNG_ID", "1565299837522673674")
    steuerung_ch = guild.get_channel(int(steuerung_ch_id)) if steuerung_ch_id and steuerung_ch_id.isdigit() else None
    if not steuerung_ch:
        for ch in guild.text_channels:
            if any(k in ch.name.lower() for k in ("admin-steuerung", "admin-konsole", "steuerung")):
                steuerung_ch = ch
                break

    if steuerung_ch:
        try:
            embed = discord.Embed(
                title="🛠️ Weihnachtsdorf 7 • Admin-Steuerungszentrale",
                description=(
                    "Willkommen in der **Admin-Leitstelle**! Hier kannst du das gesamte Projekt, Server, "
                    "Accounts, Live-Metriken und Spieler direkt per Slash-Command (`/`) oder Schnellzugriffs-Buttons steuern.\n\n"
                    "📋 **Wichtige Management-Befehle (`/`):**\n\n"
                    "🚀 **Projekt- & Server-Verwaltung:**\n"
                    "• `/live-status` — Echtzeit-Metriken (CPU, RAM, Uptime, TPS, Spielerzahl via Wings-API).\n"
                    "• `/online-spieler` — Zeigt alle aktuell verbundenen Spieler mit Rolle & Discord-Tag.\n"
                    "• `/wartung-timer <minuten> [grund]` — Startet automatisierten Countdown (Chat/Title + Auto-Kick & Lock).\n"
                    "• `/wartung-abbrechen` — Bricht den laufenden Wartungs-Countdown sofort ab.\n"
                    "• `/projekt-start` — Startet Weihnachtsdorf 7: Öffnet die Whitelist für alle registrierten Spieler.\n"
                    "• `/projekt-sperren` — Wartungsmodus aktivieren: Nimmt normale Spieler von der Whitelist.\n"
                    "• `/mc-befehl <befehl>` — Führt Konsolenbefehle via RCON aus (z. B. `/mc-befehl befehl:weather clear`).\n"
                    "• `/sync-alle` — Forciert den sofortigen Komplett-Sync aller Dienste (LDAP, Authentik, MC, Planka).\n"
                    "• `/spielerliste-aktualisieren` — Forciert die sofortige Aktualisierung der Teilnehmerliste.\n\n"
                    "👥 **Benutzer- & Moderations-Management:**\n"
                    "• `/spieler-kick <ziel> [grund]` — Kickt einen Spieler sofort vom Server.\n"
                    "• `/einladen <discord_nutzer> [rolle] [ign]` — Erstellt eine Einladung (Admin-Check aktiv).\n"
                    "• `/zugang-entziehen <ziel>` — Entzieht Zugänge (wie `wd-admin revoke --keep-ldap`).\n"
                    "• `/account-loeschen <ziel> [grund]` — Löscht einen Account spurlos aus allen Systemen (Purge).\n"
                    "• `/ban-spieler <ziel> <grund>` — Sperrt einen Spieler auf Minecraft und blockiert BlueMap (Alias: `/spieler-bannen`).\n"
                    "• `/pardon-spieler <ziel>` — Hebt die Sperre eines Spielers wieder auf (Alias: `/spieler-entbannen`).\n"
                    "• `/admin-spieler <discord_nutzer>` — Zeigt die vollständige Identitäts- und Rechtedatenakte per Discord-Auswahl an.\n"
                    "• `/admin-rundruf <nachricht> [rolle] [nutzer]` — Sendet Direktnachrichten an Rollen oder Spieler.\n"
                    "• `/passwort-reset` — Generiert einen Authentik-Passwort-Resetlink.\n"
                    "• `/status` — Prüft deinen Account-Status und Rollen.\n\n"
                    "🔑 **Spieler-Befehle (Public):**\n"
                    "• `/anmelden` — Bewerbung für Weihnachtsdorf 7 einreichen.\n"
                    "• `/abmelden` — Self-Service Abmeldung vom Projekt.\n"
                    "• `/entbannung` — Einspruch / Entbannungsantrag stellen.\n"
                    "• `/auth-code` (oder `/code`) — 6-stelligen Login-Code für Authentik anfordern.\n"
                    "• `/links` — Übersicht aller geschützten Dienste und Server-Adressen.\n\n"
                    "⚡ **Schnellzugriff (One-Click Actions):**\n"
                    "Nutze die Buttons unten, um häufige Aufgaben blitzschnell ohne Tippen auszulösen!"
                ),
                color=discord.Color.from_rgb(46, 204, 113),
            )
            embed.set_footer(text="Weihnachtsdorf 7 Identity & Operations System • WHD Privileges")

            guide_msg = None
            pins = [p async for p in steuerung_ch.pins()]
            for p in pins:
                if p.author.id == bot.user.id and "Admin-Steuerungszentrale" in (p.embeds[0].title if p.embeds else ""):
                    guide_msg = p
                    break

            if guide_msg:
                await guide_msg.edit(embed=embed, view=AdminControlDashboardView())
            else:
                msg = await steuerung_ch.send(embed=embed, view=AdminControlDashboardView())
                try:
                    await msg.pin()
                except Exception:
                    pass
            logger.info(f"Initialized admin control center in #{steuerung_ch.name}")
        except Exception as e:
            logger.warning(f"Failed to setup admin control panel: {e}")


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

@tree.command(
    name="anmelden",
    description="Reiche deine Bewerbung / Aufnahmeantrag für Weihnachtsdorf 7 ein.",
)
async def cmd_anmelden(interaction: discord.Interaction):
    channel_name = getattr(interaction.channel, "name", "")
    if channel_name != "anmeldung":
        await interaction.response.send_message(
            "❌ **Falscher Channel:** Du kannst dich ausschließlich im Kanal `#anmeldung` bewerben.",
            ephemeral=True,
        )
        return
    await trigger_application_modal(interaction)


@tree.command(
    name="abmelden",
    description="Meldet einen Spieler vom Projekt ab (Admin).",
)
@app_commands.default_permissions(administrator=True)
async def cmd_abmelden(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können diesen Befehl ausführen.", ephemeral=True)
        return
    await trigger_optout_flow(interaction)


@tree.command(
    name="account-loeschen",
    description="Löscht einen Spieler-Account spurlos und vollständig aus allen Systemen (Admin).",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    ziel="Ziel-Nutzer (@User, Discord-ID oder Account-Name)",
    grund="Optionaler Grund für die Account-Löschung",
)
async def cmd_account_loeschen(
    interaction: discord.Interaction,
    ziel: str,
    grund: Optional[str] = None,
):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können diesen Befehl ausführen.", ephemeral=True)
        return
    await trigger_purge_flow(interaction, ziel=ziel, grund=grund)


@tree.command(
    name="entbannung",
    description="Stelle einen Entbannungs- oder Einspruchsantrag.",
)
async def cmd_entbannung(interaction: discord.Interaction):
    channel_name = getattr(interaction.channel, "name", "")
    if channel_name != "entbannung":
        await interaction.response.send_message(
            "❌ **Falscher Channel:** Entbannungsanträge können nur im Kanal `#entbannung` eingereicht werden.",
            ephemeral=True,
        )
        return
    await trigger_unban_modal(interaction)


@tree.command(
    name="ban-spieler",
    description="Sperrt einen Spieler vom Minecraft-Server und der BlueMap (Admin).",
)
@app_commands.default_permissions(administrator=True)
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
    name="spieler-bannen",
    description="Alias für /ban-spieler: Sperrt einen Spieler vom Minecraft-Server und der BlueMap (Admin).",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    target="Discord-Nutzer (@User oder ID) oder Ingame-Name",
    grund="Begründung für die Sperre",
)
async def cmd_spieler_bannen(interaction: discord.Interaction, target: str, grund: Optional[str] = None):
    await cmd_ban_player(interaction, target=target, grund=grund)


@tree.command(
    name="pardon-spieler",
    description="Hebt die Sperre eines Spielers auf Minecraft & BlueMap auf (Admin).",
)
@app_commands.default_permissions(administrator=True)
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
            f"🎉 **Deine Sperre für Weihnachtsdorf 7 wurde aufgehoben!**\n\n"
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
    name="spieler-entbannen",
    description="Alias für /pardon-spieler: Hebt die Sperre eines Spielers auf Minecraft & BlueMap auf (Admin).",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(target="Discord-Nutzer (@User oder ID) oder Ingame-Name")
async def cmd_spieler_entbannen(interaction: discord.Interaction, target: str):
    await cmd_pardon_player(interaction, target=target)


@tree.command(
    name="einladen",
    description="Lädt einen neuen Spieler oder Administrator manuell zu Weihnachtsdorf 7 ein (Admin).",
)
@app_commands.default_permissions(administrator=True)
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
@app_commands.default_permissions(administrator=True)
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
    name="einladung",
    description="Alias für /einladen: Lädt einen neuen Spieler oder Administrator manuell zu Weihnachtsdorf 7 ein (Admin).",
)
@app_commands.default_permissions(administrator=True)
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
async def cmd_einladung_alias(
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
    name="passwort-reset",
    description="Fordere einen einmaligen Link zum Zurücksetzen deines Passworts an (Admin).",
)
@app_commands.default_permissions(administrator=True)
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
    name="spieler-zurücksetzen",
    description="Alias für /passwort-reset: Fordere einen einmaligen Link zum Zurücksetzen deines Passworts an (Admin).",
)
@app_commands.default_permissions(administrator=True)
async def cmd_spieler_zuruecksetzen(interaction: discord.Interaction):
    await cmd_password_reset(interaction)


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


@tree.command(
    name="auth-code",
    description="Generiert einen 6-stelligen Login-Code für Authentik (nur für registrierte Spieler).",
)
async def cmd_auth_code(interaction: discord.Interaction):
    await handle_auth_code_request(interaction)


@tree.command(
    name="code",
    description="Kurzform: Generiert einen 6-stelligen Login-Code für Authentik (nur für Spieler).",
)
async def cmd_code(interaction: discord.Interaction):
    await handle_auth_code_request(interaction)


@tree.command(
    name="projekt-start",
    description="Startet Weihnachtsdorf 7: Öffnet Whitelist für alle registrierten Spieler und schaltet BlueMap frei.",
)
@app_commands.default_permissions(administrator=True)
async def cmd_projekt_start(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren dürfen das Projekt starten.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    res = await asyncio.to_thread(mc.launch_project)

    log_admin_event(
        title="🎉 WEIHNACHTSDORF 7 GESTARTET! 🎄",
        description=f"Das Projekt wurde von {interaction.user.mention} offiziell gestartet! Whitelist für alle Teilnehmer ist aktiv.",
        color=COLOR_SUCCESS,
        fields=[
            {"name": "Status", "value": "🟢 LIVE", "inline": True},
            {"name": "Ausgeführt von", "value": interaction.user.mention, "inline": True},
        ],
        author_name=interaction.user.name,
    )

    await sync_playerlist_bot(bot, force=True)

    embed = discord.Embed(
        title="🎄 WEIHNACHTSDORF 7 WURDE GESTARTET! 🎄",
        description=(
            "**Das Projekt ist nun offiziell eröffnet!**\n\n"
            "• Die Whitelist-Schranke wurde aufgehoben.\n"
            "• Alle registrierten Spieler sind freigeschaltet und können verbinden.\n"
            "• BlueMap Live-Karte ist öffentlich zugänglich."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Gestartet von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="projekt-sperren",
    description="Versetzt den Server in Wartungsmodus (entfernt Spieler-Whitelist, Team bleibt aktiv).",
)
@app_commands.default_permissions(administrator=True)
async def cmd_projekt_sperren(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren dürfen das Projekt sperren.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    res = await asyncio.to_thread(mc.lock_project)

    log_admin_event(
        title="🔒 Projekt gesperrt (Wartungsmodus)",
        description=f"Das Projekt wurde von {interaction.user.mention} in den Wartungsmodus versetzt (Whitelist & BlueMap für Spieler gesperrt).",
        color=COLOR_WARNING,
        fields=[
            {"name": "Status", "value": "🔒 GESPERRT (Pre-Launch / Wartung)", "inline": True},
            {"name": "Ausgeführt von", "value": interaction.user.mention, "inline": True},
        ],
        author_name=interaction.user.name,
    )

    await sync_playerlist_bot(bot, force=True)

    embed = discord.Embed(
        title="🔒 Projekt gesperrt / Wartungsmodus",
        description=(
            "**Der Server befindet sich nun im Wartungsmodus.**\n\n"
            "• Reguläre Spieler wurden von der Whitelist genommen.\n"
            "• BlueMap Live-Karte ist für reguläre Spieler gesperrt (Pre-Launch Schutz).\n"
            "• Teammitglieder (Admins & Mods) behalten weiterhin Zugang zur Vorbereitung/Wartung.\n"
            "• Spieler-Verbindungen sind bis zum offiziellen Projektstart blockiert."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Gesperrt von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="zugang-entziehen",
    description="Entzieht einem Nutzer alle Projekt-Zugänge und Berechtigungen.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    ziel="Discord @Mention, ID, Minecraft-Name oder Benutzername",
    account_behalten="True = Rechte entziehen & sperren (Account behalten); False = Komplett löschen",
)
async def cmd_zugang_entziehen(
    interaction: discord.Interaction, ziel: str, account_behalten: bool = True
):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Zugänge entziehen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    clean_target = ziel.strip().replace("<@", "").replace(">", "").replace("!", "")

    if account_behalten:
        res = await asyncio.to_thread(
            revoke_user_privileges,
            target=clean_target,
            reason="Admin Slash-Command /zugang-entziehen",
            initiator=f"{interaction.user.name} ({interaction.user.mention})",
        )
        if not res.get("found"):
            await interaction.followup.send(f"⚠️ Ziel '{ziel}' konnte in der Datenbank / LDAP nicht gefunden werden.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔒 Zugang & Rechte entzogen",
            description=f"Dem Nutzer **{ziel}** (`{res.get('username')}`) wurden alle Berechtigungen entzogen.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="OpenLDAP", value="Rollen & Gruppen entfernt (Account bleibt erhalten)", inline=True)
        embed.add_field(name="Authentik / SSO", value="Deaktiviert & BlueMap gesperrt", inline=True)
        embed.add_field(name="Minecraft", value="Whitelist & Permissions entfernt", inline=True)
        embed.add_field(name="Ausgeführt von", value=interaction.user.mention, inline=False)
        embed.set_footer(text="Weihnachtsdorf 7 Identity System")
        await interaction.followup.send(embed=embed)
    else:
        ldap = LDAPClient()
        authentik = AuthentikClient()
        planka = PlankaSyncClient()
        mc = MinecraftSyncClient()

        user = ldap.search_by_discord(clean_target) if clean_target.isdigit() else None
        if not user:
            user = ldap.get_user(clean_target) or ldap.search_by_discord(clean_target) or ldap.search_by_minecraft(clean_target)

        username = user["uid"] if user else clean_target
        ign = user.get("minecraftName") if user else clean_target
        mc_uuid = user.get("minecraftUuid") if user else None

        if user:
            ldap.delete_user(username)
        authentik.delete_user(username)
        authentik.delete_invitations(username)
        planka.delete_user(username)
        if ign:
            mc.revoke_player_privileges(ign, mc_uuid=mc_uuid)
            mc.execute_commands([f"whitelist remove {ign}", f"lp user {ign} clear", f'kick {ign} "Zugang vollständig gelöscht"'])

        log_admin_event(
            title="🗑️ Account vollständig gelöscht (Purge)",
            description=f"Account **{clean_target}** (`{username}`) wurde über `/zugang-entziehen` gelöscht.",
            color=COLOR_ERROR,
            fields=[
                {"name": "Ziel", "value": clean_target, "inline": True},
                {"name": "Ausgeführt von", "value": interaction.user.mention, "inline": True},
            ],
            author_name=interaction.user.name,
        )
        await sync_playerlist_bot(bot, force=True)

        embed = discord.Embed(
            title="🗑️ Account vollständig gelöscht",
            description=f"Der Account **{clean_target}** (`{username}`) wurde vollständig aus OpenLDAP, Authentik, Planka und Minecraft gelöscht.",
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed)


@tree.command(
    name="admin-rundruf",
    description="Sendet eine Direktnachricht an alle Mitglieder einer Rolle oder einen bestimmten Nutzer.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    nachricht="Der Inhalt der Nachricht",
    rolle="Zielgruppe (all, player, staff, admin, mod)",
    nutzer="Optional: Bestimmter Nutzer (@Mention, ID oder Name)",
)
@app_commands.choices(
    rolle=[
        app_commands.Choice(name="Alle Teilnehmer (all)", value="all"),
        app_commands.Choice(name="Nur Spieler (player)", value="player"),
        app_commands.Choice(name="Gesamtes Team (staff)", value="staff"),
        app_commands.Choice(name="Nur Admins (admin)", value="admin"),
        app_commands.Choice(name="Nur Moderatoren (mod)", value="mod"),
    ]
)
async def cmd_admin_rundruf(
    interaction: discord.Interaction,
    nachricht: str,
    rolle: Optional[app_commands.Choice[str]] = None,
    nutzer: Optional[str] = None,
):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren dürfen Rundrufe versenden.", ephemeral=True)
        return

    clean_msg = nachricht.strip()
    if not clean_msg:
        await interaction.response.send_message("❌ Bitte gib eine Nachricht an.", ephemeral=True)
        return

    if not rolle and not nutzer:
        await interaction.response.send_message("❌ Bitte gib entweder eine Zielrolle oder einen bestimmten Nutzer an.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    ldap = LDAPClient()
    recipients = []
    target_label = ""

    if nutzer:
        clean_user = nutzer.strip().replace("<@", "").replace(">", "").replace("!", "")
        u = ldap.search_by_discord(clean_user) or ldap.get_user(clean_user)
        target_label = f"Nutzer `{clean_user}`"
        recipients.append(clean_user if not u else (u.get("discordId") or u.get("discordName") or u["uid"]))
    elif rolle:
        role_val = rolle.value
        target_label = f"Rolle `{role_val}`"
        admins, mods = ldap.get_admins_and_mods()
        players = ldap.get_players()

        if role_val == "admin":
            user_list = admins
        elif role_val == "mod":
            user_list = mods
        elif role_val == "staff":
            user_list = admins + mods
        elif role_val == "player":
            user_list = players
        else:
            user_list = admins + mods + players

        for r in user_list:
            handle = r.get("discordId") or r.get("discordName") or r.get("uid")
            if handle and handle not in recipients:
                recipients.append(handle)

    if not recipients:
        await interaction.followup.send(f"⚠️ Keine aktiven Empfänger für {target_label} gefunden.", ephemeral=True)
        return

    dm_text = (
        f"📢 **Wichtige Mitteilung • Weihnachtsdorf 7** 🎄\n\n"
        f"{clean_msg}\n\n"
        f"*Gesendet von Administrator {interaction.user.name} via Admin-Steuerung.*"
    )

    success_count = 0
    fail_count = 0

    for rec in recipients:
        sent = await asyncio.to_thread(send_dm, rec, dm_text)
        if sent:
            success_count += 1
        else:
            fail_count += 1

    log_admin_event(
        title="📢 Rundruf versendet",
        description=f"Administrator {interaction.user.mention} hat einen Rundruf an **{target_label}** versendet.",
        color=COLOR_SUCCESS if fail_count == 0 else COLOR_WARNING,
        fields=[
            {"name": "Empfänger-Ziel", "value": target_label, "inline": True},
            {"name": "Zugestellt", "value": f"✅ {success_count} / {len(recipients)}", "inline": True},
            {"name": "Nachricht", "value": f"{clean_msg[:500]}..." if len(clean_msg) > 500 else clean_msg, "inline": False},
        ],
        author_name=interaction.user.name,
    )

    embed = discord.Embed(
        title="📢 Rundruf abgeschlossen",
        description=f"Nachricht an **{target_label}** wurde versendet.",
        color=discord.Color.green() if fail_count == 0 else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Erfolgreich", value=f"✅ {success_count}", inline=True)
    embed.add_field(name="Fehlgeschlagen", value=f"⚠️ {fail_count}", inline=True)
    embed.add_field(name="Nachricht", value=clean_msg, inline=False)
    embed.set_footer(text=f"Initiiert von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="mc-befehl",
    description="Führt einen Konsolenbefehl direkt auf dem Minecraft-Server via RCON/Wings aus.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    befehl="Minecraft-Konsolenbefehl (z. B. weather clear, time set day, whitelist list, tps)"
)
async def cmd_mc_befehl(interaction: discord.Interaction, befehl: str):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren dürfen Minecraft-Serverbefehle ausführen.", ephemeral=True)
        return

    clean_cmd = befehl.strip()
    if clean_cmd.startswith("/"):
        clean_cmd = clean_cmd[1:].strip()

    if not clean_cmd:
        await interaction.response.send_message("❌ Bitte gib einen gültigen Konsolenbefehl an.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    ok = await asyncio.to_thread(mc.execute_commands, [clean_cmd])

    log_admin_event(
        title="💻 Minecraft-Konsolenbefehl ausgeführt",
        description=f"Administrator {interaction.user.mention} hat einen Konsolenbefehl ausgeführt.",
        color=COLOR_INFO,
        fields=[
            {"name": "Befehl", "value": f"`/{clean_cmd}`", "inline": True},
            {"name": "Status", "value": "✅ Gesendet" if ok else "❌ Fehler bei Ausführung", "inline": True},
            {"name": "Ausgeführt von", "value": interaction.user.mention, "inline": True},
        ],
        author_name=interaction.user.name,
    )

    embed = discord.Embed(
        title="💻 Minecraft Konsole",
        description=f"Befehl: `/{clean_cmd}`",
        color=discord.Color.green() if ok else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Status", value="✅ Befehl erfolgreich an Server gesendet." if ok else "❌ Übertragungsfehler zur Server-Konsole.", inline=False)
    embed.set_footer(text=f"Ausgeführt von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="sync-alle",
    description="Führt einen sofortigen Komplett-Sync aller Dienste (LDAP, Authentik, Minecraft, Planka) durch.",
)
@app_commands.default_permissions(administrator=True)
async def cmd_sync_alle(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren dürfen die Synchronisierung ausführen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    mc_res = await asyncio.to_thread(mc.sync_all, True)
    auth_ok = await asyncio.to_thread(authentik.trigger_ldap_sync)
    list_ok = await sync_playerlist_bot(bot, force=True)

    embed = discord.Embed(
        title="🔄 Vollständige System-Synchronisierung",
        description="Alle Dienste wurden erfolgreich mit OpenLDAP abgeglichen.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Minecraft & Whitelist", value="✅ Synchronisiert" if mc_res else "⚠️ Fehler", inline=True)
    embed.add_field(name="Authentik IdP", value="✅ LDAP-Task getriggert" if auth_ok else "⚠️ Fehler", inline=True)
    embed.add_field(name="Live-Spielerliste", value="✅ Embed aktualisiert" if list_ok else "⚠️ Unverändert", inline=True)
    embed.set_footer(text=f"Ausgeführt von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="spielerliste-aktualisieren",
    description="Aktualisiert sofort das interaktive Live-Board in #teilnehmerliste (Admin).",
)
@app_commands.default_permissions(administrator=True)
async def cmd_spielerliste_aktualisieren(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können die Spielerliste manuell aktualisieren.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok = await sync_playerlist_bot(bot, force=True)
    if ok:
        await interaction.followup.send("✅ Das Teilnehmerliste-Board wurde erfolgreich aktualisiert.", ephemeral=True)
    else:
        await interaction.followup.send("⚠️ Teilnehmerliste konnte nicht aktualisiert werden.", ephemeral=True)


@tree.command(
    name="admin-spieler",
    description="Detaillierte Account- und Statusabfrage eines Spielers über Discord oder Textsuche.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    discord_nutzer="Discord-Benutzer direkt auswählen (empfohlen – findet den Account garantiert)",
    suchbegriff="Alternativ: Freitext-Suche nach Minecraft IGN, LDAP-UID oder Discord-ID",
)
async def cmd_admin_spieler(
    interaction: discord.Interaction,
    discord_nutzer: Optional[discord.User] = None,
    suchbegriff: Optional[str] = None,
):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Spielerdaten abfragen.", ephemeral=True)
        return

    if not discord_nutzer and not suchbegriff:
        await interaction.response.send_message(
            "❌ Bitte wähle entweder einen Discord-Benutzer aus (`discord_nutzer`) oder gib einen Suchbegriff ein (`suchbegriff`).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    ldap = LDAPClient()
    authentik = AuthentikClient()

    user = None
    target_discord_id = None
    target_discord_name = None

    if discord_nutzer:
        target_discord_id = str(discord_nutzer.id)
        target_discord_name = discord_nutzer.name
        # 1. Search by Discord Snowflake ID
        user = ldap.search_by_discord(target_discord_id)
        # 2. Search by Discord username
        if not user:
            user = ldap.search_by_discord(discord_nutzer.name)
        # 3. Search by Discord display name
        if not user and discord_nutzer.display_name:
            user = ldap.search_by_discord(discord_nutzer.display_name)
    elif suchbegriff:
        clean = suchbegriff.strip().replace("<@", "").replace(">", "").replace("!", "")
        # 1. If pure digit: Discord ID
        if clean.isdigit():
            target_discord_id = clean
            user = ldap.search_by_discord(clean)

        # 2. Search members in current guild for matching name/display_name
        if not user and interaction.guild:
            matched_member = discord.utils.find(
                lambda m: m.name.lower() == clean.lower() or m.display_name.lower() == clean.lower(),
                interaction.guild.members,
            )
            if matched_member:
                target_discord_id = str(matched_member.id)
                target_discord_name = matched_member.name
                user = ldap.search_by_discord(target_discord_id) or ldap.search_by_discord(target_discord_name)

        # 3. Direct lookup in LDAP
        if not user:
            user = ldap.get_user(clean) or ldap.search_by_discord(clean) or ldap.search_by_minecraft(clean)

    # Check SQLite status
    query_id = target_discord_id if target_discord_id else (user.get("discordId") if user else None)
    status = applications.get_user_status(query_id) if query_id else {}

    if not user:
        if discord_nutzer or target_discord_id:
            d_name = discord_nutzer.name if discord_nutzer else (target_discord_name or suchbegriff)
            d_id = target_discord_id or (str(discord_nutzer.id) if discord_nutzer else "-")
            mention = discord_nutzer.mention if discord_nutzer else f"<@{target_discord_id}>"

            embed = discord.Embed(
                title=f"👤 Profil: {d_name} (Nicht in LDAP)",
                description=f"Für {mention} (`{d_id}`) existiert noch **kein fertiger OpenLDAP-Account**.",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="OpenLDAP Status", value="❌ Kein aktiver Account hinterlegt", inline=False)
            embed.add_field(name="Bewerbungs-DB", value=f"Gesperrt: `{status.get('is_banned', False)}` | Cooldown: `{status.get('in_cooldown', False)}`", inline=False)
            if status.get("minecraft_ign"):
                embed.add_field(name="Registrierter IGN", value=f"`{status.get('minecraft_ign')}`", inline=True)
            embed.set_footer(text=f"Abgerufen von {interaction.user.name}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        search_label = discord_nutzer.name if discord_nutzer else (suchbegriff or "Unbekannt")
        await interaction.followup.send(
            f"❌ Kein Benutzer zu '{search_label}' in OpenLDAP oder in der Bewerbungsdatenbank gefunden.",
            ephemeral=True,
        )
        return

    username = user["uid"]
    ign = user.get("minecraftName") or "Nicht hinterlegt"
    mc_uuid = user.get("minecraftUuid") or "Nicht hinterlegt"
    discord_name = user.get("discordName") or "-"
    discord_id = user.get("discordId") or "-"
    email = user.get("mail") or "-"

    admin_dns = set(ldap.get_group_members(ldap.admin_group_dn))
    mod_dns = set(ldap.get_group_members(ldap.mod_group_dn))
    player_dns = set(ldap.get_group_members(ldap.player_group_dn))

    groups = []
    if user["dn"] in admin_dns:
        groups.append("wd_admins")
    if user["dn"] in mod_dns:
        groups.append("wd_mods")
    if user["dn"] in player_dns:
        groups.append("wd_players")
    if not groups:
        groups.append("Keine (Inaktiv / Quarantäne)")

    auth_user = authentik.get_user_by_username(username)
    auth_active = auth_user.get("is_active", False) if auth_user else "Kein Auth-User"
    auth_banned = (auth_user.get("attributes") or {}).get("is_banned", False) if auth_user else False

    embed = discord.Embed(
        title=f"👤 Profil: {username}",
        description=f"Vollständige technische Identitätsübersicht für **{username}**.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Minecraft IGN", value=f"`{ign}`", inline=True)
    embed.add_field(name="Minecraft UUID", value=f"`{mc_uuid}`", inline=True)
    discord_display = f"<@{discord_id}> ({discord_name})" if discord_id != "-" else f"`{discord_name}`"
    embed.add_field(name="Discord", value=discord_display, inline=True)
    embed.add_field(name="E-Mail", value=f"`{email}`", inline=True)
    embed.add_field(name="LDAP Gruppen", value=f"`{', '.join(groups)}`", inline=True)
    embed.add_field(name="Authentik Status", value=f"Aktiv: `{auth_active}` | Ban: `{auth_banned}`", inline=True)
    embed.add_field(name="Bewerbungs-DB", value=f"Gesperrt: `{status.get('is_banned', False)}` | Cooldown: `{status.get('in_cooldown', False)}`", inline=True)
    embed.set_footer(text=f"Abgerufen von {interaction.user.name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(
    name="live-status",
    description="Echtzeit-Hardware- und Servermetriken (CPU, RAM, Disk, Uptime, TPS, Spielerzahl).",
)
@app_commands.default_permissions(administrator=True)
async def cmd_live_status(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können den Live-Status abrufen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    tel = await asyncio.to_thread(mc.get_server_telemetry)
    tps = await asyncio.to_thread(mc.get_live_tps)
    online_count, max_p, players = await asyncio.to_thread(mc.get_online_players)
    is_live = mc.is_project_launched()

    state = tel.get("state", "unknown").upper()
    cpu = tel.get("cpu_percent", 0.0)
    ram_used = tel.get("memory_used_mb", 0.0)
    ram_max = tel.get("memory_max_mb", 0.0)
    ram_pct = tel.get("memory_percent", 0.0)
    disk_used = tel.get("disk_used_mb", 0.0)
    uptime_s = tel.get("uptime_seconds", 0)

    days = uptime_s // 86400
    hours = (uptime_s % 86400) // 3600
    minutes = (uptime_s % 3600) // 60
    uptime_str = f"{days}d {hours}h {minutes}m" if days > 0 else f"{hours}h {minutes}m"

    embed = discord.Embed(
        title="📊 Minecraft Live-Servermetriken",
        description=f"Status: **{state}** | Modus: **{'🟢 LIVE (Offen)' if is_live else '🔒 WARTUNG (Gesperrt)'}**",
        color=discord.Color.green() if state == "RUNNING" else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="⚡ TPS (5s, 1m, 5m, 15m)", value=f"`{tps}`", inline=False)
    embed.add_field(name="👥 Spieler Online", value=f"**{online_count}** / {max_p}", inline=True)
    embed.add_field(name="⏱️ Server-Uptime", value=f"`{uptime_str}`", inline=True)
    embed.add_field(name="🖥️ CPU-Last", value=f"`{cpu}%`", inline=True)
    embed.add_field(name="🧠 RAM-Verbrauch", value=f"`{ram_used:.0f} MB` / `{ram_max:.0f} MB` ({ram_pct}%)", inline=True)
    embed.add_field(name="💾 Festplatte", value=f"`{disk_used:.0f} MB`", inline=True)
    embed.add_field(name="🎮 Engine", value="`Purpur 26.3 (Java 25)`", inline=True)
    embed.set_footer(text=f"Abgerufen von {interaction.user.name} • Pterodactyl Wings Telemetry")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="online-spieler",
    description="Zeigt alle aktuell auf dem Minecraft-Server verbundenen Spieler mit Discord-Verknüpfung.",
)
@app_commands.default_permissions(administrator=True)
async def cmd_online_spieler(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können die Spielerliste abrufen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    ldap = LDAPClient()
    count, max_p, names = await asyncio.to_thread(mc.get_online_players)

    admins, mods = ldap.get_admins_and_mods()
    admin_uids = {a.get("minecraftName", "").lower() for a in admins if a.get("minecraftName")}
    mod_uids = {m.get("minecraftName", "").lower() for m in mods if m.get("minecraftName")}

    embed = discord.Embed(
        title=f"👥 Aktuell verbundene Spieler ({count}/{max_p})",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not names:
        embed.description = "Aktuell befindet sich **kein Spieler** auf dem Server."
    else:
        lines = []
        for ign in names:
            ign_clean = ign.strip()
            u = ldap.search_by_minecraft(ign_clean)
            discord_tag = f"<@{u['discordId']}>" if (u and u.get("discordId")) else (f"`{u['discordName']}`" if (u and u.get("discordName")) else "*Nicht verknüpft*")
            
            if ign_clean.lower() in admin_uids:
                badge = "👑 `Admin`"
            elif ign_clean.lower() in mod_uids:
                badge = "🛡️ `Mod`"
            else:
                badge = "🎮 `Spieler`"

            lines.append(f"• **{ign_clean}** — {badge} — Discord: {discord_tag}")
        embed.description = "\n".join(lines)

    embed.set_footer(text=f"Abgerufen von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="spieler-kick",
    description="Kickt einen Spieler sofort mit Begründung vom Minecraft-Server.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    ziel="Minecraft-Name oder Discord @Mention / ID",
    grund="Begründung für den Kick (erscheint auf dem Kick-Screen)",
)
async def cmd_spieler_kick(interaction: discord.Interaction, ziel: str, grund: Optional[str] = "Vom Administrator gekickt"):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können Spieler kicken.", ephemeral=True)
        return

    clean_target = ziel.strip().replace("<@", "").replace(">", "").replace("!", "")
    clean_reason = (grund or "Vom Administrator gekickt").strip()

    ldap = LDAPClient()
    user = None
    if clean_target.isdigit():
        user = ldap.search_by_discord(clean_target)
    if not user:
        user = ldap.get_user(clean_target) or ldap.search_by_discord(clean_target) or ldap.search_by_minecraft(clean_target)

    ign = user.get("minecraftName") if user else clean_target
    if not ign:
        await interaction.response.send_message(f"❌ Für '{ziel}' konnte kein Minecraft Ingame-Name ermittelt werden.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    mc = MinecraftSyncClient()
    kick_cmd = f'kick {ign} "{clean_reason}"'
    ok = await asyncio.to_thread(mc.execute_commands, [kick_cmd])

    log_admin_event(
        title="👢 Spieler vom Server gekickt",
        description=f"Spieler **{ign}** wurde von {interaction.user.mention} vom Minecraft-Server gekickt.",
        color=COLOR_WARNING,
        fields=[
            {"name": "Minecraft-Name", "value": f"`{ign}`", "inline": True},
            {"name": "Begründung", "value": clean_reason, "inline": True},
            {"name": "Gekickt von", "value": interaction.user.mention, "inline": True},
        ],
        author_name=interaction.user.name,
    )

    embed = discord.Embed(
        title="👢 Spieler gekickt",
        description=f"Befehl für **{ign}** wurde an den Server übermittelt.",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Minecraft-Name", value=f"`{ign}`", inline=True)
    embed.add_field(name="Begründung", value=clean_reason, inline=True)
    embed.add_field(name="Status", value="✅ Ausgeführt" if ok else "⚠️ Übertragungsfehler", inline=True)
    embed.set_footer(text=f"Ausgeführt von {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@tree.command(
    name="wartung-timer",
    description="Startet einen automatisierten Countdown für geplante Serverwartung.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    minuten="Countdown-Dauer in Minuten (z. B. 1, 5, 10, 15)",
    grund="Optionale Begründung für den Wartungs-Kick",
)
@app_commands.choices(
    minuten=[
        app_commands.Choice(name="1 Minute (Schnelltest)", value=1),
        app_commands.Choice(name="2 Minuten", value=2),
        app_commands.Choice(name="5 Minuten (Standard)", value=5),
        app_commands.Choice(name="10 Minuten", value=10),
        app_commands.Choice(name="15 Minuten", value=15),
        app_commands.Choice(name="30 Minuten", value=30),
    ]
)
async def cmd_wartung_timer(
    interaction: discord.Interaction,
    minuten: app_commands.Choice[int],
    grund: Optional[str] = "Geplante Serverwartung",
):
    global active_maintenance
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können den Wartungs-Timer starten.", ephemeral=True)
        return

    if active_maintenance:
        await interaction.response.send_message(
            "⚠️ Es läuft bereits ein Wartungs-Countdown! Nutze `/wartung-abbrechen` oder den roten Button, um ihn zu stoppen.",
            ephemeral=True,
        )
        return

    dur = minuten.value
    clean_reason = grund.strip() if grund else "Geplante Serverwartung"

    embed = discord.Embed(
        title="⏳ Geplante Serverwartung • Countdown gestartet",
        description=(
            f"**Dauer:** `{dur}` Minuten\n"
            f"**Grund:** {clean_reason}\n"
            f"**Gestartet von:** {interaction.user.mention}\n\n"
            f"📢 *Warnungen werden wiederholt im Minecraft-Chat & Title gesendet.*\n"
            f"🔒 *Bei 0 Minuten werden alle Spieler gekickt und der Server automatisch gesperrt.*"
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Klicke unten, um den Countdown vorzeitig abzubrechen.")
    view = MaintenanceAbortView()

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()

    task = asyncio.create_task(
        run_maintenance_countdown(
            duration_minutes=dur,
            reason=clean_reason,
            initiator=interaction.user,
            message=msg,
        )
    )

    active_maintenance = {
        "task": task,
        "message": msg,
        "end_time": datetime.now(timezone.utc) + timedelta(minutes=dur),
        "reason": clean_reason,
        "initiator": interaction.user,
    }

    log_admin_event(
        title="⏳ Wartungs-Countdown gestartet",
        description=f"Administrator {interaction.user.mention} hat einen Wartungs-Timer über **{dur} Minuten** gestartet.",
        color=COLOR_WARNING,
        fields=[
            {"name": "Dauer", "value": f"{dur} Minuten", "inline": True},
            {"name": "Grund", "value": clean_reason, "inline": True},
        ],
        author_name=interaction.user.name,
    )


@tree.command(
    name="wartung-abbrechen",
    description="Bricht einen aktuell laufenden Wartungs-Countdown sofort ab.",
)
@app_commands.default_permissions(administrator=True)
async def cmd_wartung_abbrechen(interaction: discord.Interaction):
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("❌ Nur Administratoren können den Countdown abbrechen.", ephemeral=True)
        return

    await cancel_maintenance_countdown(interaction)


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
async def on_member_join(member: discord.Member):
    """
    Triggered when a member joins the Discord server.
    If the user already has an existing project account in LDAP (which was deactivated when they left),
    alerts admins in #admin-logs with an interactive [Account reaktivieren] button.
    """
    if settings.DISCORD_GUILD_ID and member.guild.id != int(settings.DISCORD_GUILD_ID):
        return

    if member.bot:
        return

    logger.info(f"Member joined guild {member.guild.name}: {member.name} (ID: {member.id}, Display: {member.display_name})")

    # Search for existing account in LDAP
    ldap = LDAPClient()
    user = await asyncio.to_thread(ldap.search_by_discord, str(member.id))
    if not user:
        user = await asyncio.to_thread(ldap.search_by_discord, member.name)
    if not user and member.display_name != member.name:
        user = await asyncio.to_thread(ldap.search_by_discord, member.display_name)

    if not user:
        try:
            log_admin_event(
                title="🟢 Neues Mitglied beigetreten",
                description=f"**{member.name}** ({member.mention}) ist dem Discord-Server beigetreten.\n*(Neuer Nutzer, noch kein Projekt-Account)*",
                color=COLOR_INFO,
                fields=[
                    {"name": "Discord-Nutzer", "value": f"{member.name} (`{member.id}`)", "inline": True},
                ],
                author_name="Discord Gateway",
            )
        except Exception as e:
            logger.error(f"Error logging new member join: {e}")
        return

    # Existing account found!
    username = user["uid"]
    ign = user.get("minecraftName")
    mc_uuid = user.get("minecraftUuid")

    logger.info(f"Departed member {member.name} rejoined! Found existing account: {username} (IGN: {ign})")

    view = ReclaimAccountView(target_uid=username, discord_id=str(member.id), ign=ign or "")

    embed = discord.Embed(
        title="🔄 Ehemaliger Nutzer wieder beigetreten",
        description=(
            f"**{member.name}** ({member.mention}) ist dem Discord-Server wieder beigetreten!\n"
            f"Sein Projekt-Account existiert bereits im System, ist aber derzeit **deaktiviert** (ohne Berechtigungen)."
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Account (UID)", value=f"`{username}`", inline=True)
    embed.add_field(name="Discord", value=f"{member.name} (`{member.id}`)", inline=True)
    embed.add_field(name="Minecraft IGN", value=f"`{ign}`" if ign else "*(Kein IGN hinterlegt)*", inline=True)
    embed.add_field(
        name="Aktion erforderlich",
        value="Klicke unten auf **„Account reaktivieren“**, um seine Whitelist und Rechte mit einem Klick wiederherzustellen.",
        inline=False,
    )
    embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")

    logs_ch_id = getattr(settings, "DISCORD_CHANNEL_LOGS_ID", "")
    if logs_ch_id:
        channel = member.guild.get_channel(int(logs_ch_id))
        if channel:
            await channel.send(embed=embed, view=view)


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
    bot.add_view(AuthCodeButtonView())
    bot.add_view(AppealStartView())
    bot.add_view(ManualInviteStartView())
    bot.add_view(AdminControlDashboardView())
    bot.add_view(MaintenanceAbortView())

    # Sync slash commands (guild-only to avoid duplicate global preview)
    try:
        if settings.DISCORD_GUILD_ID:
            guild_obj = discord.Object(id=int(settings.DISCORD_GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced_guild = await tree.sync(guild=guild_obj)
            logger.info(f"Synced {len(synced_guild)} slash commands to guild {settings.DISCORD_GUILD_ID}")
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
