# Weihnachtsdorf 7 • Identity, SSO, LDAP & Discord Architecture Guide

**Version**: 1.0.0  
**Datum**: 2026-10-27  
**Status**: Produktiv & Verifiziert ✅  

---

## 1. Übersicht & Zielsetzung

Für das Event **Minecraft SMP Weihnachtsdorf 7** wurde ein zentrales, hochsicheres Single-Sign-On- (SSO) und Berechtigungs-Ökosystem aufgebaut. Alle administrativen Webdienste, das Kanban-Board, das Game-Panel sowie die Ingame-Rechte auf dem Minecraft-Server werden über eine zentrale "Single Source of Truth" (OpenLDAP) gesteuert.

### Kernfunktionen:
1. **Einladungs-Workflow ohne Vorwissen**:
   Der Serverbetreiber lädt ein Teammitglied mit einem einzigen Befehl ein (`wd-admin invite <discord_name>`). Der Minecraft-Name muss beim Einladen **nicht** bekannt sein.
2. **Discord DM Dispatcher**:
   Der Discord-Bot **WHD Privileges** sucht das Teammitglied serverübergreifend und stellt einen einmaligen, 24 Stunden gültigen Registrierungs-Link per privater Nachricht (DM) zu.
3. **Sicheres Onboarding**:
   Das Teammitglied setzt sein zentrales Passwort, richtet verpflichtend **2-Faktor-Authentifizierung (TOTP)** ein und trägt seinen eigenen Minecraft Ingame-Namen (IGN) ein.
4. **Automatische Freischaltung & Sync**:
   - Validierung des Minecraft-Namens und Ermittlung der echten UUID über die offizielle Mojang-API.
   - Speicherung aller Identitätsdaten im OpenLDAP (`wdAccount` Schema).
   - Automatische Provisionierung und Kennwort-Aktualisierung im Planka Kanban-Board (PostgreSQL).
   - Automatische Freischaltung auf der Minecraft-Whitelist und Zuweisung der LuckPerms-Gruppe (`admin` oder `mod`).
   - Zusendung einer Begrüßungs-DM via Discord mit allen Portal-Links.
5. **Passwort-Self-Service**:
   Teammitglieder können ihr Passwort jederzeit per Discord Slash-Befehl (`/passwort-reset`) oder durch Admins via `wd-admin reset <name>` zurücksetzen.
6. **Proxmox- & Homelab-Schutz**:
   Bestehende Homelab- und Proxmox-Rechte (z. B. für `tigimax`) werden persistent geschützt und bei jedem Sync-Vorgang garantiert.

---

## 2. Architektur & Datenfluss

```
                               ┌────────────────────────────────────────────────────────┐
                               │                    Teammitglied                        │
                               └───────────┬────────────────────────────────┬───────────┘
                                           │ 1. Klickt Einladungslink       │ 7. Erhält Willkommens-DM
                                           ▼                                │    & Links via Discord
┌────────────────────────────────────────────────────────────────────────┐  │
│                   Authentik IdP (auth.tigimax.mooo.com)               │  │
│  ├─ Invitation Enrollment Flow (24h Token)                             │  │
│  ├─ Passwort setzen & TOTP 2FA Einrichtung                             │  │
│  ├─ Prompt Stage: "Minecraft In-Game Name (IGN)"                       │  │
│  └─ Expression Policy: wd-enrollment-complete-policy                   │  │
└──────────────────────────────────┬─────────────────────────────────────┘  │
                                   │ 2. HTTP POST Webhook                   │
                                   ▼                                        │
┌────────────────────────────────────────────────────────────────────────┐  │
│                   wd-identity Service (idp-01:8000)                   │  │
│  ├─ 3. Mojang API: Validiert IGN & ermittelt canonical UUID           │  │
│  ├─ 4. OpenLDAP: Schreibt minecraftName, UUID & userPassword           │──┘
│  ├─ 5. Planka Sync: Idempotenter PostgreSQL bcrypt User-Sync           │
│  ├─ 6. Minecraft Sync: Setzt Whitelist & LuckPerms via Wings API       │
│  └─ 7. Discord Bot Dispatcher: Sendet Welcome-Guide per DM             │
└───────┬──────────────────────────┬───────────────────────────┬─────────┘
        │                          │                           │
        ▼                          ▼                           ▼
┌──────────────────┐      ┌──────────────────┐      ┌───────────────────────────┐
│ OpenLDAP (VM105) │      │ Planka (usrv01)  │      │ Purpur Server (VM120)     │
│ ldaps://:636     │      │ PostgreSQL DB    │      │ Wings Node 2 API (:8080)  │
│ Single Source    │      │ user_account     │      │ Whitelist & LuckPerms     │
│ of Truth         │      │ SSO Gatekeeper   │      │ Gruppe admin/mod          │
└──────────────────┘      └──────────────────┘      └───────────────────────────┘
```

---

## 3. Host- & Netzwerk-Topologie

| Host / VM | IP-Adresse | Betriebssystem / Engine | Aufgaben & Dienste |
|---|---|---|---|
| **`anode01`** (VM 105) | `192.168.1.164` | Ubuntu 24.04 LTS (slapd) | **OpenLDAP 2.5.20** (LDAPS Port 636), Schemas `wdAccount`, Gruppen `wd_admins`, `wd_mods`, `proxmox_admins`, `homelab_admins` |
| **`idp-01`** (VM 121) | `192.168.1.150` | Ubuntu 24.04 LTS (Docker) | **Authentik 2026.8.5** (:9000), **`wd-identity.service`** (:8000), **`wd-bot.service`**, **`mc-admin-sync.timer`** |
| **`usrv01`** | `192.168.1.174` | Ubuntu 22.04 LTS | **Nginx Reverse Proxy** & Ingress, Let's Encrypt TLS, Forward-Auth Outpost Gate, **Planka Kanban** (:3000), Kernel DNAT (25565/24454) |
| **`ulxc01`** | `192.168.1.173` | Ubuntu 22.04 LTS (LXC) | **Pi-hole DNS** (Port 53), Split-DNS für `*.tigimax.mooo.com` & `.net` Zone (Ansible-gesteuert) |
| **`mc-node1`** (VM 119) | `192.168.1.148` | Ubuntu 24.04 LTS | **Velocity Proxy 3.5.1** (:25565), **BlueMap CLI Server** (:8123), Pterodactyl Panel v1.15.2, Wings Node 1, MariaDB 10.11, Redis |
| **`mc-main`** (VM 120) | `192.168.1.149` | Ubuntu 24.04 LTS | **Purpur 26.3 Build 2654** auf Java 25 LTS (:25574), Plan Analytics (:8804), Wings Node 2 (:8080) |
| **`pnode01`** | `192.168.1.199` | Proxmox VE 8.x | Proxmox Cluster Node 1 (LDAP-Realm `ldap`, Host für VM 119 `mc-node1`) |
| **`pnode02`** | `192.168.1.200` | Proxmox VE 8.x | Proxmox Cluster Node 2 (Host für VM 120 `mc-main`, i5-9400F 6 Kerne dediziert) |
| **`pnode03`** | `192.168.1.201` | Proxmox VE 8.x | Proxmox Cluster Node 3 (Quorum & Failover) |

---

## 4. Öffentliche Domains & Forward-Auth Schutz

Alle administrativen Webanwendungen befinden sich hinter dem zentralen Authentik Forward-Auth Gate auf `usrv01`. Nicht authentifizierte Zugriffe werden automatisch per HTTP 302 auf `https://auth.tigimax.mooo.com` umgeleitet. Durch die gemeinsame Cookie-Domain `.tigimax.mooo.com` genügt ein einmaliger Login, um alle Portale zu nutzen.

| Dienst | Öffentliche URL | Backend-Ziel | Schutzmechanismus |
|---|---|---|---|
| **SSO Portal** | `https://auth.tigimax.mooo.com` | `idp-01:9000` | Authentik IdP (Let's Encrypt TLS) |
| **Live-Karte** | `https://map.tigimax.mooo.com` | `mc-node1:8123` | Nginx Forward-Auth (`wd-bluemap` Provider) |
| **Server-Analytics** | `https://plan.tigimax.mooo.com` | `mc-main:8804` | Nginx Forward-Auth (`wd-plan` Provider) |
| **Game-Panel** | `https://panel.tigimax.mooo.com` | `mc-node1:80` | Nginx Forward-Auth (`wd-panel` Provider), Wings-API `/api/remote/` im Bypass |
| **Kanban-Board** | `https://tigimax.mooo.com` | `usrv01:3000` | Nginx Forward-Auth (`wd-planka` Provider) + interner DB-Sync |
| **Minecraft Server** | `tigimax.mooo.com:25565` | `mc-node1:25565` | Purpur Whitelist & LuckPerms Berechtigungsgruppen |

> [!IMPORTANT]
> **Pterodactyl Wings Daemon Bypass**:  
> Der Daemon auf Node 2 kommuniziert mit dem Panel über `panel.tigimax.mooo.com/api/remote/` unter Verwendung eines Bearer-Tokens. In der Nginx-Konfiguration ist dieser Pfad explizit mit `auth_request off;` konfiguriert, damit interne Daemon-Heartbeats und Server-Installationen nicht durch Cookie-Redirects blockiert werden.

---

## 5. OpenLDAP Schema-Erweiterung (`wdAccount`)

Auf `anode01` wurde das benutzerdefinierte Schema `wdAccount` (`1.3.6.1.4.1.99999.1`) installiert:

```ldif
dn: cn=wdaccount,cn=schema,cn=config
objectClass: olcSchemaConfig
cn: wdaccount
olcAttributeTypes: ( 1.3.6.1.4.1.99999.1.1 NAME 'discordName' DESC 'Discord Username or Tag' EQUALITY caseIgnoreMatch SYNTAX 1.3.6.1.4.1.1466.115.121.1.15 SINGLE-VALUE )
olcAttributeTypes: ( 1.3.6.1.4.1.99999.1.2 NAME 'discordId' DESC 'Discord Snowflake User ID' EQUALITY numericStringMatch SYNTAX 1.3.6.1.4.1.1466.115.121.1.36 SINGLE-VALUE )
olcAttributeTypes: ( 1.3.6.1.4.1.99999.1.3 NAME 'minecraftName' DESC 'Minecraft In-Game Name' EQUALITY caseIgnoreMatch SYNTAX 1.3.6.1.4.1.1466.115.121.1.15 SINGLE-VALUE )
olcAttributeTypes: ( 1.3.6.1.4.1.99999.1.4 NAME 'minecraftUuid' DESC 'Minecraft Player UUID' EQUALITY caseIgnoreMatch SYNTAX 1.3.6.1.4.1.1466.115.121.1.15 SINGLE-VALUE )
olcObjectClasses: ( 1.3.6.1.4.1.99999.1.100 NAME 'wdAccount' DESC 'Weihnachtsdorf 7 Linked Account' SUP top AUXILIARY MAY ( discordName $ discordId $ minecraftName $ minecraftUuid ) )
```

- **Overlays**: Die Overlays `memberof` und `refint` (Referential Integrity) sind aktiv. Wird ein Benutzer aus einer Gruppe entfernt oder gelöscht, bereinigt LDAP alle Referenzen automatisch.
- **Service Accounts**:
  - `uid=svc-authentik,ou=ServiceAccounts,dc=ldap-01,dc=net`: Schreib- und Leserechte auf `ou=User` und `ou=Groups`.
  - `uid=svc-readonly,ou=ServiceAccounts,dc=ldap-01,dc=net`: Leserechte für Proxmox VE und Guacamole.

---

## 6. Discord Bot (`WHD Privileges`)

- **Application Name**: `WHD Privileges`
- **Client ID**: `1564751378436456468`
- **Intents**: `Server Members Intent` und `Message Content Intent` aktiviert.
- **Multi-Guild Discovery**: Der Bot durchsucht automatisch alle Server, auf denen er Mitglied ist (z. B. `Minecraft Weihnachtsdorf` ID `784805552504438794` und `Weihnachtsdorf` ID `909437200125538364`), um Mitglieder anhand ihres Namens oder Nicks zielsicher aufzulösen.
- **Slash Commands**:
  - `/passwort-reset`: Erzeugt einen Einmal-Wiederherstellungslink und schickt ihn dem Teammitglied per DM.
  - `/status`: Zeigt die aktuelle Rolle, verknüpften Minecraft-Namen, UUID und 2FA-Status an.
  - `/links`: Übersicht aller geschützten Dienste und Server-Adressen.

---

## 7. CLI-Administration (`wd-admin`)

Das CLI-Werkzeug [`wd-admin`](../tools/wd-identity/wd_identity/cli.py) (siehe auch [wd-identity Dokumentation](../tools/wd-identity/README.md)) kann direkt im Terminal auf dem Mac (`~/.local/bin/wd-admin`) oder per SSH auf `idp-01` ausgeführt werden.

### Befehlsübersicht:

#### 1. Teammitglied einladen
```bash
wd-admin invite <discord_name> [--role admin|mod]
```
- *Beispiel*: `wd-admin invite MaxMustermann`
- Legt den LDAP-Benutzer an.
- Generiert einen 24h gültigen Authentik-Einladungslink.
- Der Bot schickt die Einladung automatisch per Discord-DM.

#### 2. Passwort zurücksetzen
```bash
wd-admin reset <discord_name>
```
- *Beispiel*: `wd-admin reset MaxMustermann`
- Erzeugt einen zeitlich begrenzten Einmal-Recoverylink und sendet ihn per DM.

#### 3. Team-Übersicht anzeigen
```bash
wd-admin list
```
- Listet alle aktiven Admins und Moderatoren mit Rolle, Discord-Tag, Minecraft-IGN und UUID auf.

#### 4. Berechtigungen entziehen (Revoke)
```bash
wd-admin revoke <discord_name>
```
- Entfernt den Benutzer aus den LDAP-Gruppen (`wd_admins` / `wd_mods`).
- Deaktiviert das Konto im Planka Kanban-Board.
- Entfernt den Spieler von der Minecraft-Whitelist und löscht die LuckPerms-Gruppe.

#### 5. Manuelle Minecraft-Synchronisation
```bash
wd-admin sync [--force]
```
- Gleicht den aktuellen Stand aller LDAP-Admins mit der Minecraft-Whitelist und LuckPerms ab.
- Dank State-Tracking werden nur dann Konsolenbefehle an den Server gesendet, wenn sich tatsächlich etwas geändert hat (kein Konsolen-Spam).

---

## 8. Systemd-Dienste auf `idp-01`

Auf VM 121 (`idp-01`) laufen drei gehärtete Hintergrunddienste:

1. **`wd-identity.service`**:
   FastAPI/Uvicorn Webhook-Dienst auf Port `8000`. Nimmt Authentik-Events entgegen, validiert Mojang-Profile, provisioniert Planka und steuert den Welcome-DM-Versand.
2. **`wd-bot.service`**:
   Permanenter Discord-Gateway-Client. Registriert und beantwortet die Slash-Commands `/passwort-reset`, `/status` und `/links`.
3. **`mc-admin-sync.timer`**:
   Trigger-Timer, der alle 60 Sekunden `mc-admin-sync.service` ausführt, um eventuelle LDAP-Änderungen vollautomatisch und idempotent auf den Purpur-Server zu spiegeln.

---

## 9. Fehlerbehebung & Runbook

### Problem: Einladungslink meldet "Internal Server Error /setup"
- **Ursache**: Authentik prüft intern das System-Flag `Setup`.
- **Lösung**: Im Authentik-Container folgenden Befehl ausführen:
  ```bash
  docker exec -i authentik-server-1 ak shell -c "from authentik.core.setup.views import Setup; Setup.set(True)"
  ```

### Problem: Benutzer meldet "Erlaubnis verweigert" beim zweiten Login
- **Ursache**: Der Benutzer wurde im Enrollment als `external` angelegt.
- **Lösung**: Benutzer auf `internal` umstellen:
  ```bash
  docker exec -i authentik-server-1 ak shell -c "from authentik.core.models import User, UserTypes; u = User.objects.get(username='<NAME>'); u.type = UserTypes.INTERNAL; u.path = 'goauthentik.io/sources/openldap'; u.save()"
  ```

### Problem: Discord DM kann nicht zugestellt werden
- **Prüfen**:
  1. Ist der Benutzer auf einem der Discord-Server des Bots anwesend?
  2. Hat der Benutzer DMs von Servermitgliedern in seinen Discord-Privatsphäre-Einstellungen aktiviert?
  3. Alternativ: Die Discord-User-ID (Snowflake-ID, z. B. `320166607748071424`) anstelle des Namens übergeben:
     ```bash
     wd-admin invite 320166607748071424
     ```

---

## 10. Relevante Handbücher & Dokumentation

- 📖 [Admin & Operations Handbook](admin_operations_handbook.md) – Umfassendes Betriebshandbuch für Admins, Troubleshooting & Spark Profiling.
- 🌐 [Network, DNS & Firewall Guide](network_dns_firewall_guide.md) – Ingress-Netzwerk, Kernel DNAT, Pi-hole DNS & UFW Firewall-Regeln.
- 🦖 [Phase 2 Pterodactyl Deployment Report](phase2_pterodactyl_deployment_report.md) – Deployment-Abschlussbericht und Leistungsdaten.
