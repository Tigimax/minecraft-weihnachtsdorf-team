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
  - Slash Commands: `/passwort-reset`, `/status`, `/links`.
  - Multi-Guild Discovery: Durchsucht automatisch alle Server des Bots, um Nutzer anhand ihres Namens, Nicks oder ihrer Snowflake-ID aufzulösen.
  - Synchroner REST-Dispatcher für 100% zuverlässige DM-Zustellung aus CLI, Webhooks und Systemd-Diensten.
- **Minecraft Privilege Sync**:
  - Mojang API-Client zur kanonischen Auflösung von Spielername und offizieller UUID.
  - Pterodactyl Wings Node 2 API Integration (`192.168.1.149:8080`).
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
│   ├── authentik_client.py       # API Client für Authentik Invitations, Users & Recovery
│   ├── cli.py                    # Click CLI Implementierung (wd-admin)
│   ├── config.py                 # Pydantic Settings & Environment Parsing
│   ├── discord_bot.py            # Discord Bot & REST DM Dispatcher
│   ├── ldap_client.py            # OpenLDAP Client mit wdAccount Schema & Group Management
│   ├── mc_sync.py                # Wings API Client & LuckPerms/Whitelist Synchronizer
│   ├── mojang_client.py          # Mojang API Resolver mit Fallback
│   ├── planka_sync.py            # Planka PostgreSQL Sync Client
│   ├── utils.py                  # Input-Sanitization (LDAP Injection, SQL Escaping, IGN Validation)
│   └── webhook.py                # FastAPI Webhook Server für Authentik Enrollment Triggers
├── pyproject.toml
└── README.md
```

---

## CLI-Nutzung (`wd-admin`)

### 1. Teammitglied einladen
```bash
wd-admin invite <discord_name> [--role admin|mod]
```
*Beispiel*:
```bash
wd-admin invite MaxMustermann
```
- Erstellt den LDAP-Account.
- Erzeugt einen 24h gültigen Authentik-Einladungslink.
- Der Bot sendet den Registrierungslink per Discord-DM.
- Der Nutzer setzt Passwort, 2FA und Minecraft-Namen selbst.

### 2. Passwort zurücksetzen
```bash
wd-admin reset <discord_name>
```
*Beispiel*:
```bash
wd-admin reset MaxMustermann
```
- Erzeugt einen Einmal-Link zur Passwort-Wiederherstellung und schickt ihn per Discord-DM.

### 3. Team-Übersicht anzeigen
```bash
wd-admin list
```
Gibt eine tabellarische Übersicht aller Admins und Moderatoren mit Rolle, Discord-Tag, Minecraft-IGN und UUID aus.

### 4. Berechtigungen entziehen (Revoke)
```bash
wd-admin revoke <discord_name>
```
- Entfernt LDAP-Gruppen (`wd_admins`/`wd_mods`).
- Deaktiviert den Nutzer in Planka.
- Entfernt den Spieler von der Minecraft-Whitelist und löscht LuckPerms-Rechte.

### 5. Minecraft-Synchronisation anstoßen
```bash
wd-admin sync [--force]
```
Gleicht alle LDAP-Mitglieder mit der Minecraft-Whitelist und LuckPerms ab. Sendet nur bei Änderungen Konsolenbefehle.

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
