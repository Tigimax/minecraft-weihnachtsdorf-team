"""Discord channel discovery, setup, and persistent pin management."""

import logging
from typing import Optional

import discord

from wd_identity.discord_views import (
    AdminControlDashboardView,
    AppealStartView,
    ApplyStartView,
    AuthCodeButtonView,
    ManualInviteStartView,
)

logger = logging.getLogger(__name__)


def _bot():
    import sys
    main = sys.modules.get("__main__")
    if main is not None and getattr(main, "__spec__", None) and main.__spec__.name == "wd_identity.discord_bot":
        return main
    from wd_identity import discord_bot
    return discord_bot


async def resolve_admin_channel(guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
    """Resolves the admin review channel by configured ID or matching name."""
    if not guild:
        guild = _bot().bot.get_guild(int(_bot().settings.DISCORD_GUILD_ID)) if _bot().settings.DISCORD_GUILD_ID else None
    if not guild:
        return None

    admin_channel_id = getattr(_bot().settings, "DISCORD_CHANNEL_ADMIN_ID", "")
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
    if not _bot().settings.DISCORD_GUILD_ID:
        return

    guild = _bot().bot.get_guild(int(_bot().settings.DISCORD_GUILD_ID))
    if not guild:
        logger.warning(f"Configured guild {_bot().settings.DISCORD_GUILD_ID} not found in bot cache.")
        return

    category_id = getattr(_bot().settings, "DISCORD_CATEGORY_WD7_ID", "1561436206577750138")
    category = guild.get_channel(int(category_id)) if category_id and category_id.isdigit() else None

    # Bot permissions check
    me = guild.me
    can_manage_channels = me.guild_permissions.manage_channels or me.guild_permissions.administrator

    # 1. Anmeldung Channel
    anmeldung_ch = None
    anmeldung_id = getattr(_bot().settings, "DISCORD_CHANNEL_ANMELDUNG_ID", "")
    if anmeldung_id and anmeldung_id.isdigit():
        c = guild.get_channel(int(anmeldung_id))
        if isinstance(c, discord.TextChannel):
            anmeldung_ch = c

    if not anmeldung_ch:
        for ch in guild.text_channels:
            if any(k in ch.name.lower() for k in ("anmeldung", "anmeldungen")):
                anmeldung_ch = ch
                if not category or ch.category_id == category.id:
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
                "2. Gib deinen Minecraft Ingame-Namen (IGN) sowie eine kurze Nachricht an das Team ein.\n"
                "3. Dein Antrag wird vertraulich an die Administratoren übermittelt.\n"
                "4. Nach der Annahme wirst du direkt für den Minecraft-Server und das Webportal freigeschaltet und erhältst eine Bestätigung per Direktnachricht (DM).\n\n"
                "🤖 **Wichtiger Hinweis zu KI-Bewerbungen:**\n"
                "Alle eingehenden Anträge werden vor der Bearbeitung automatisch durch einen KI-Detector geprüft. KI-generierte Texte (z. B. ChatGPT) werden **automatisch und ausnahmslos abgelehnt**!\n\n"
                "🔐 **Login im Webportal:**\n"
                "Sobald du freigeschaltet bist, kannst du dich im Webportal anmelden. Wenn du nach dem 2FA-Code gefragt wirst, sendet dir dieser Bot den 6-stelligen Login-Code automatisch per Direktnachricht (DM) zu!"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Weihnachtsdorf 7 Identity System • WHD Privileges")

        guide_msg = None
        pins = [p async for p in anmeldung_ch.pins()]
        for p in pins:
            if p.author.id == _bot().bot.user.id and "Anmeldung für Weihnachtsdorf 7" in (p.embeds[0].title if p.embeds else ""):
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
    entbannung_id = getattr(_bot().settings, "DISCORD_CHANNEL_ENTBANNUNG_ID", "")
    if entbannung_id and entbannung_id.isdigit():
        c = guild.get_channel(int(entbannung_id))
        if isinstance(c, discord.TextChannel):
            entbannung_ch = c

    if not entbannung_ch:
        for ch in guild.text_channels:
            if any(k in ch.name.lower() for k in ("entbannung", "entbannungsanträge", "entbannungsantrag")):
                entbannung_ch = ch
                if not category or ch.category_id == category.id:
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
        has_guide = any(p.author.id == _bot().bot.user.id and "Entbannungs- & Einspruchsanträge" in (p.embeds[0].title if p.embeds else "") for p in pins)
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
            admin_role = guild.get_role(int(getattr(_bot().settings, "DISCORD_ADMIN_ROLE_ID", "909437200125538371")))
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
    invite_ch_id = getattr(_bot().settings, "DISCORD_CHANNEL_INVITES_ID", "")
    invite_ch = guild.get_channel(int(invite_ch_id)) if invite_ch_id and invite_ch_id.isdigit() else None
    if not invite_ch:
        for ch in guild.text_channels:
            if any(k in ch.name.lower() for k in ("admin-einladungen", "admin-invites", "einladungen-admin")):
                invite_ch = ch
                break

    if invite_ch:
        try:
            pins = [p async for p in invite_ch.pins()]
            has_panel = any(p.author.id == _bot().bot.user.id and "Manuelle Einladungs-Zentrale" in (p.embeds[0].title if p.embeds else "") for p in pins)
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
    steuerung_ch_id = getattr(_bot().settings, "DISCORD_CHANNEL_STEURUNG_ID", "1565299837522673674")
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
                if p.author.id == _bot().bot.user.id and "Admin-Steuerungszentrale" in (p.embeds[0].title if p.embeds else ""):
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


