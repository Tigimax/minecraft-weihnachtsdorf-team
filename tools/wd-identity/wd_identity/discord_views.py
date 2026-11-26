"""Discord UI Modals and Persistent Views for Weihnachtsdorf 7."""

import asyncio
import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


def _bot():
    import sys
    main = sys.modules.get("__main__")
    if main is not None and getattr(main, "__spec__", None) and main.__spec__.name == "wd_identity.discord_bot":
        return main
    from wd_identity import discord_bot
    return discord_bot


class ApplicationModal(discord.ui.Modal, title="Weihnachtsdorf 7 • Anmeldung"):
    ign_input = discord.ui.TextInput(
        label="Minecraft Ingame-Name (IGN)",
        placeholder="z. B. Steve",
        min_length=1,
        max_length=16,
        required=True,
    )
    message_input = discord.ui.TextInput(
        label="Deine Nachricht an das Team",
        style=discord.TextStyle.paragraph,
        placeholder="Wer bist du und warum möchtest du mitmachen? Bitte in eigenen Worten verfassen.",
        min_length=10,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw_ign = (self.ign_input.value or "").strip()
        if not _bot().validate_minecraft_ign(raw_ign):
            await interaction.response.send_message(
                f"❌ **Ungültiger Ingame-Name:** '{raw_ign}' entspricht nicht den Minecraft-Richtlinien "
                f"(1-16 Zeichen, nur Buchstaben, Zahlen und Unterstriche).",
                ephemeral=True,
            )
            return

        try:
            canonical_ign, _ = await asyncio.to_thread(_bot().get_player_profile, raw_ign, require_existing=True)
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ **Minecraft-Account nicht gefunden:** {e}",
                ephemeral=True,
            )
            return
        except Exception as e:
            logger.warning(f"Mojang verification error for '{raw_ign}': {e}")
            await interaction.response.send_message(
                f"⚠️ **Fehler bei der Mojang-Überprüfung:** Die Mojang API konnte '{raw_ign}' nicht verifizieren ({e}). "
                f"Bitte versuche es in wenigen Augenblicken erneut.",
                ephemeral=True,
            )
            return

        await _bot().handle_application_submit(interaction, self.message_input.value, minecraft_ign=canonical_ign)


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
        await _bot().handle_unban_submit(interaction, self.statement_input.value)


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
        await _bot().handle_admin_reject_submit(interaction, self.app_id, self.reason_input.value)


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
        await _bot().handle_admin_appeal_reject_submit(
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
        await _bot().handle_manual_invite_submit(
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
        if not _bot().is_admin_user(interaction.user):
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
        if not _bot().is_admin_user(interaction.user):
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

        ldap = _bot().LDAPClient()
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

        _bot().log_admin_event(
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
        if not _bot().is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        mc = _bot().MinecraftSyncClient()
        await asyncio.to_thread(mc.launch_project)
        _bot().log_admin_event(
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
        if not _bot().is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        mc = _bot().MinecraftSyncClient()
        await asyncio.to_thread(mc.lock_project)
        _bot().log_admin_event(
            title="🔒 Projekt gesperrt (via Dashboard)",
            description=f"Administrator {interaction.user.mention} hat das Projekt in den Wartungsmodus versetzt.",
            color=COLOR_WARNING,
            author_name=interaction.user.name,
        )
        await sync_playerlist_bot(bot, force=True)
        await interaction.followup.send("🔒 **Projekt gesperrt!** Server ist im Wartungsmodus (nur Team).", ephemeral=True)

    @discord.ui.button(
        label="Sync forcieren",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="wd_ctrl_sync_btn",
    )
    async def btn_sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _bot().is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können diese Aktion ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        mc = _bot().MinecraftSyncClient()
        authentik = _bot().AuthentikClient()
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
        if not _bot().is_admin_user(interaction.user):
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
        if not _bot().is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können Rundrufe senden.", ephemeral=True)
            return
        await interaction.response.send_modal(BroadcastModal())

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
        if not _bot().is_admin_user(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können den Countdown abbrechen.", ephemeral=True)
            return
        await _bot().cancel_maintenance_countdown(interaction)

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
        await _bot().trigger_application_modal(interaction)


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
        await _bot().handle_auth_code_request(interaction)


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
        await _bot().trigger_unban_modal(interaction)


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

        ldap = _bot().LDAPClient()
        user = ldap.search_by_discord_id(discord_id)
        status = _bot().applications.get_user_status(discord_id)

        if not user and not status.get("has_pending"):
            await interaction.followup.send(
                "❌ Du bist aktuell nicht als Teilnehmer registriert und hast keine offene Bewerbung.",
                ephemeral=True,
            )
            return

        res = await asyncio.to_thread(
            _bot().revoke_user_privileges,
            target=user if user else discord_id,
            reason=f"Selbstabmeldung: {user_reason}",
            initiator=f"{interaction.user.name} (Selbstabmeldung)",
        )

        ign = res.get("minecraft_name")
        uid = res.get("username") or interaction.user.name

        try:
            _bot().log_admin_event(
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
            _bot().purge_user_completely,
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
