"""Welcome and onboarding message templates for Weihnachtsdorf 7 Discord Bot."""

from wd_identity.config import settings


def get_mod_welcome_message(username: str, ign: str = "") -> str:
    ign_line = f"• Ingame-Name: `{ign}`\n" if ign else ""
    return f"""🛡️ **Willkommen im Moderatoren-Team von Weihnachtsdorf 7, {username}!** 🛡️

Du wurdest als **Moderator** freigeschaltet. Als Moderator bist du vor allem direkt auf dem Minecraft-Server als Ansprechpartner da und kannst bei Problemen, Fragen oder Regelverstößen eingreifen.

🎮 **Deine Ingame-Aufgaben & Werkzeuge:**
{ign_line}• **Hilfe & Support:** Spieler bei Problemen oder Streitigkeiten ingame unterstützen.
• **CoreProtect (`/co inspect`, `/co rollback`):** Griefing oder Diebstahl aufklären und beheben.
• **WorldEdit (`//wand`):** Schnelle Reparaturen und Unterstützung bei Bauarbeiten.
• **Moderation:** `/tp`, `/tphere`, `/invsee`, `/vanish`, `/fly`, `/kick`, `/warn`, `/mute`, `/ban`.

⏰ **Server- & Map-Zugang:**
• Regulär kannst du den Minecraft-Server und die Live-Karte mit dem offiziellen Projektstart betreten.
• Falls du vorab bei Vorbereitungen helfen sollst, kann dir ein Admin temporäre Baurechte für ein Zeitfenster freischalten.

📌 **Wichtige Hinweise:**
• Um administrative Aufgaben (wie Bewerbungen oder Servereinstellungen) brauchst du dich nicht zu kümmern – dein Fokus liegt voll auf dem Spielgeschehen.
• Bei heiklen Situationen oder Unklarheiten wende dich bitte immer direkt an die Admins.

Vielen Dank für deine Unterstützung im Weihnachtsdorf! 🎅✨"""


def get_welcome_message(username: str, role: str, ign: str) -> str:
    if (role or "").lower() == "mod":
        return get_mod_welcome_message(username, ign=ign)

    return f"""🎄 **Willkommen im Weihnachtsdorf 7 Team, {username}!** 🎄

Dein Zugang als **{role.upper()}** wurde erfolgreich eingerichtet.

📌 **Wichtige Zugänge & Portale (Single Sign-On):**
• **Authentik SSO Portal:** {settings.AUTHENTIK_BASE_URL}
• **BlueMap Live-Karte:** https://map.tigimax.com
• **Plan Analytics:** https://plan.tigimax.com
• **Pterodactyl Panel:** https://panel.tigimax.com
• **Planka Kanban Board:** https://kanban.tigimax.com
• **Minecraft Server:** `tigimax.com` (Purpur 26.3)

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
• **BlueMap Live-Karte:** https://map.tigimax.com
• **Minecraft Server:** `tigimax.com` (Purpur 26.3)

🎮 **Dein Minecraft Profil:**
• Ingame-Name: `{ign}`
• Whitelist-Status: {whitelist_status}

🔒 **Zwei-Faktor-Authentifizierung (2FA):**
• Du kannst bei jedem Login frei wählen:
  1. **Discord-Bot Einmalcode (Empfohlen):** Nutze `/auth-code` (oder `/code`) hier im Discord, um dir sekundenschnell einen 6-stelligen Login-Code generieren zu lassen.
  2. **Authenticator-App (TOTP):** Falls eingerichtet, kannst du alternativ jederzeit den Code deiner Authenticator-App (z. B. Google Authenticator, 2FAS) eingeben.
• Bei Passwort-Verlust kannst du jederzeit `/passwort-reset` nutzen.

Wir freuen uns auf ein unvergessliches Event mit dir! ✨"""


