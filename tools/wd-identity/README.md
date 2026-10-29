# wd-identity • Weihnachtsdorf 7 Identity, SSO & Privilege Sync

Das Paket **`wd-identity`** bildet das zentrale administrative Steuerungs-Backend für das Event **Minecraft SMP Weihnachtsdorf 7**. Es verbindet OpenLDAP, Authentik IdP, Discord Bot, Planka Kanban und den Minecraft Purpur Server zu einem homogenen, sicheren Single-Sign-On- und Rollenverwaltungs-System.

---

## Features

- **OpenLDAP Integration**:
  - LDAP3-basierte Verwaltung unter `dc=ldap-01,dc=net`.
  - Schema `wdAccount` (`discordName`, `discordId`, `minecraftName`, `minecraftUuid`).
  - Automatische Zuordnung zu `cn=wd_admins` und `cn=wd_mods`.
  - Garantierter Schutz und Persistierung für Homelab- und Proxmox-Superadmins (`proxmox_admins`, `proxmox_users`, `guac_admins`, `homelab_admins`).
- **Authentik SSO IdP**:
  - Automatisierte Erstellung von 24h gültigen Einladungslinks (`/if/flow/invitation-enrollment-flow/`).
  - Erzwingung von sicheren Passwörtern und verpflichtender **2FA (TOTP/WebAuthn)**.
  - Abfrage des Minecraft Ingame-Namens (IGN) direkt im Webformular.
  - Generierung von zeitlich begrenzten Einmal-Recoverylinks für Passwort-Resets.
- **Discord Bot (`WHD Privileges`)**:
  - **22 Guild Slash Commands**: Vollständige Suite zur Server-, Moderations- und Projektsteuerung (`/projekt-start`, `/live-status`, `/spieler-kick`, `/admin-rundruf`, etc.).
  - **Admin Command Center (`#🎛admin-steuerung`)**: Interaktives Steuerungs-Panel mit Direktzugriff und Befehlserklärungen.
  - **Antragsprüfung (`#📋admin-anträge`)**: Interaktive Abarbeitung von Aufnahme- und Entbannungsanträgen (`[✅ Annehmen]`, `[❌ Ablehnen & Sperre setzen]` mit Cooldown-Modal).
  - **Audit Logger (`#📜admin-logs`)**: Zentrales, synchrones REST-basiertes Event-Logging (`audit_logger.py`).
  - **Membership Lifecycle (`membership.py`)**: Automatischer Privilegien-Entzug bei Server-Leave (`on_member_remove`) und interaktive Reaktivierung bei Rejoin (`on_member_join`).
  - **Player Self-Service (`#📝anmeldung`)**: Bewerbungs- und Abmelde-Modal (`/anmelden`, `/abmelden`).
  - Multi-Guild Discovery: Durchsucht automatisch alle Server des Bots, um Nutzer anhand ihres Namens, Nicks oder ihrer Snowflake-ID aufzulösen.
  - Synchroner REST-Dispatcher für 100% zuverlässige DM-Zustellung aus CLI, Webhooks und Systemd-Diensten.
- **Minecraft Privilege Sync & Telemetrie**:
  - Mojang API-Client zur kanonischen Auflösung von Spielername und offizieller UUID.
  - Pterodactyl Wings Node 2 API Integration (`192.168.1.149:8080`).
  - Live-Telemetrie (`get_server_telemetry()`), Live-TPS (`get_live_tps()`), Online-Spielerliste (`get_online_players()`).
  - Ingame-Broadcasts (`broadcast_ingame()` für Chat, Actionbar und Titel).
  - Steuerung von `whitelist add/remove` und LuckPerms Parent-Gruppen (`admin`/`mod`).
  - Idempotentes State-Tracking (`mc_sync_state.json`) verhindert Konsolen-Spam und Lock-Contention.
- **Planka Kanban Provisioning**:
  - Idempotente Benutzer-Synchronisation via SSH in den Docker Postgres-Container auf `usrv01`.
  - Automatische bcrypt-Kennwort-Synchronisation und Deaktivierung bei Rechte-Entzug.

---

## Verzeichnisstruktur

```
tools/wd-identity/
├── bin/
│   ├── mc-admin-sync             # CLI Wrapper für Minecraft-Sync Timer
│   ├── wd-admin                  # Server-seitiger CLI Wrapper (/usr/local/bin/wd-admin)
│   └── wd-admin-remote           # Workstation/Mac Wrapper für SSH Remote-Aufruf
├── etc/
│   └── default/
│       └── wd-identity           # Vorlage für /etc/default/wd-identity (Tokens, Passwörter)
├── systemd/
│   ├── mc-admin-sync.service     # Systemd Service für zyklischen Minecraft Sync
│   ├── mc-admin-sync.timer       # Systemd Timer (triggert alle 60s)
│   ├── wd-bot.service            # Systemd Service für Discord Gateway Bot
│   └── wd-identity.service       # Systemd Service für FastAPI Webhook Server (:8000)
├── tests/
│   ├── test_config.py
│   ├── test_mc_sync_logic.py
│   ├── test_mojang.py
│   └── test_utils.py
├── wd_identity/
│   ├── __init__.py
│   ├── applications.py           # SQLite Lifecycle für Bewerbungen, Entbannungen & Cooldowns
│   ├── audit_logger.py           # Zentraler REST Logger für #📜admin-logs
│   ├── authentik_client.py       # API Client für Authentik Invitations, Users & Recovery
│   ├── cli.py                    # Click CLI Implementierung (wd-admin)
│   ├── config.py                 # Pydantic Settings & Environment Parsing
│   ├── discord_bot.py            # Discord Bot, 22 Slash Commands & Persistent Views
│   ├── invitations.py            # Einladungs-Generator & Permissions-Guard
│   ├── ldap_client.py            # OpenLDAP Client mit wdAccount Schema & Group Management
│   ├── mc_sync.py                # Wings API Client, Telemetrie, TPS & RCON Broadcast
│   ├── membership.py             # Automatischer Rechteentzug bei Server-Leave & Rejoin
│   ├── mojang_client.py          # Mojang API Resolver mit Fallback
│   ├── playerlist.py             # Teilnehmerlisten-Aggregator für #📝teilnehmerliste
│   ├── planka_sync.py            # Planka PostgreSQL Sync Client
│   ├── utils.py                  # Input-Sanitization (LDAP Injection, SQL Escaping, IGN Validation)
│   └── webhook.py                # FastAPI Webhook Server für Authentik Enrollment Triggers
├── pyproject.toml
└── README.md
```

---

## CLI-Nutzung (`wd-admin`)

### 1. Benutzer / Spieler einladen
```bash
wd-admin invite <discord_name> [--role player|mod|admin] [--ign <ign>]
```
*Beispiele*:
```bash
# Regulären Spieler vorab einladen (Default-Rolle: player)
wd-admin invite GamerMax

# Administrator oder Moderator einladen
wd-admin invite MaxMustermann --role admin
```
- Erstellt den LDAP-Account in `ou=User,dc=ldap-01,dc=net` und ordnet die entsprechende Gruppe zu (`cn=wd_players`, `cn=wd_admins` oder `cn=wd_mods`).
- Erzeugt einen 24h gültigen Authentik-Einladungslink.
- Der Discord-Bot `WHD Privileges` sendet den Registrierungslink per DM.
- Der Nutzer setzt Passwort, 2FA (TOTP) und optional seinen Minecraft In-Game Namen selbst.
- **Pre-Launch Schutz**: Reguläre Spieler erhalten vor dem offiziellen Projektstart noch **keinen** Whitelist-Zugang und werden im Pre-Registered-Status gehalten.

### 2. Projekt-Status & Whitelist-Launch Gate
```bash
# Status abfragen (Launch-Modus, Staff- & Spielerzahlen)
wd-admin project status

# Projekt offiziell starten (schaltet alle registrierten Spieler sofort frei)
wd-admin project launch

# Server wieder in den Wartungs-/Pre-Launch-Modus sperren
wd-admin project lock
```

### 3. Passwort zurücksetzen
```bash
wd-admin reset <discord_name>
```
*Beispiel*:
```bash
wd-admin reset MaxMustermann
```
- Erzeugt einen Einmal-Link zur Passwort-Wiederherstellung und schickt ihn per Discord-DM.

### 4. Übersicht aller Accounts anzeigen
```bash
wd-admin list
```
Gibt eine tabellarische Übersicht aller Staff-Mitglieder sowie aller regulären Spieler mit Rolle, Discord-Tag, Minecraft-IGN, UUID und Whitelist-Status (`[PRE-REGISTERED]` vs. `[WHITELISTED]`) aus.

### 5. Benachrichtigungen per Bot senden (Notify / Broadcast)
```bash
# Nachricht an alle Admins senden
wd-admin notify --role admin "Hallo Team, hier ist der Link zum Git: https://github.com/Tigimax/minecraft-weihnachtsdorf-team"

# Alternativ als Kurzform:
wd-admin notify admin "Hallo Admins!"
wd-admin notify staff "Wichtige Team-Info..."
wd-admin notify players "Der Server öffnet heute um 18:00 Uhr!"

# Nachricht an einen einzelnen Benutzer / Discord-Tag senden:
wd-admin notify nummernfeld "Hier ist dein persönlicher Einladungslink: https://..."
```
- Sendet Direct Messages (DMs) über den offiziellen Discord-Bot `WHD Privileges` aus.
- Unterstützt Rollengruppen (`--role admin`, `mod`, `player`, `staff`, `all`) oder einzelne Discord-/LDAP-Benutzernamen.
- Fragt bei Rollen-Broadcasts interaktiv nach Bestätigung (kann mit `--yes` / `-y` übersprungen werden).

### 6. Berechtigungen entziehen (Revoke)
```bash
wd-admin revoke <discord_name>
```
- Entfernt LDAP-Gruppen (`wd_admins`/`wd_mods`/`wd_players`).
- Deaktiviert den Nutzer in Planka (falls vorhanden).
- Entfernt den Spieler von der Minecraft-Whitelist und setzt LuckPerms auf `default`.

### 6. Minecraft-Synchronisation anstoßen
```bash
wd-admin sync [--force]
```
Gleicht alle LDAP-Mitglieder mit der Minecraft-Whitelist und LuckPerms ab. Sendet nur bei Änderungen Konsolenbefehle. Regular Players werden nur synchronisiert, wenn das Projekt live geschaltet ist (`wd-admin project launch`).

---

## Lokaler Aufruf auf dem Mac

Auf dem Mac des Administrators befindet sich ein Wrapper in `~/.local/bin/wd-admin`:
```bash
wd-admin invite <discord_name>
```
Dieser leitet die Befehle automatisch per SSH transparent an `idp-01` (`192.168.1.150`) weiter.

---

## Systemd-Dienste auf `idp-01`

| Dienst | Befehl zum Prüfen / Neustarten |
|---|---|
| Webhook Server | `sudo systemctl status wd-identity` / `sudo systemctl restart wd-identity` |
| Discord Bot | `sudo systemctl status wd-bot` / `sudo systemctl restart wd-bot` |
| Sync Timer | `sudo systemctl status mc-admin-sync.timer` |

---

## Tests ausführen

Mit `uv` (schnellste & isolierte Methode):
```bash
uv run --directory tools/wd-identity python -m unittest discover -s tests -v
```

Oder klassisch mit virtuellem Environment / PYTHONPATH:
```bash
PYTHONPATH=tools/wd-identity python3 -m unittest discover -s tools/wd-identity/tests/ -v
```
Alle 18 Unittests für Input-Sanitization, SQL-Escaping, Mojang-Resolution und Idempotenz-Logik laufen vollständig durch.

---

## 📚 Verwandte Dokumentationen

- 🔐 [Identity, SSO, LDAP & Discord Guide](../../docs/identity_sso_ldap_discord_guide.md) – Architektur, OpenLDAP-Schema und Onboarding-Flow.
- 📖 [Admin & Operations Handbook](../../docs/admin_operations_handbook.md) – CLI-Befehlsreferenz, Troubleshooting & Recovery.
- 🌐 [Network, DNS & Firewall Guide](../../docs/network_dns_firewall_guide.md) – Ingress, TLS, Pi-hole Split-DNS & Portfreigaben.
