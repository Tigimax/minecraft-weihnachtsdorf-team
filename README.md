# Minecraft SMP Weihnachtsdorf 7

Infrastructure-as-Code, Serverkonfigurationen, Tuning, Identity Management und Migrations-Playbooks für das **Minecraft SMP Weihnachtsdorf 7** (80-Spieler Event am Spawn).

---

## 1. Gesamt-Architektur

```
┌────────────────────────────────────── Internet ──────────────────────────────────────┐
│  • Minecraft Server:        tigimax.mooo.com :25565 (TCP) / VoiceChat :24454 (UDP)   │
│  • Authentik SSO IdP:       https://auth.tigimax.mooo.com                            │
│  • BlueMap Live-Karte:      https://map.tigimax.mooo.com                             │
│  • Plan Analytics:          https://plan.tigimax.mooo.com                            │
│  • Pterodactyl Game Panel:  https://panel.tigimax.mooo.com                           │
│  • Planka Kanban Board:     https://tigimax.mooo.com                                 │
└──────────────────────────────────────────┬───────────────────────────────────────────┘
                                           │
                                           ▼
┌─────────────────────────────────── usrv01 (.174) ────────────────────────────────────┐
│  Nginx Ingress, TLS Termination (Let's Encrypt) & Forward-Auth Gatekeeper:           │
│  ├─ auth.tigimax.mooo.com -> idp-01 :9000 (Authentik Web)                           │
│  ├─ map.tigimax.mooo.com  -> Forward-Auth -> mc-node1 :8123 (BlueMap CLI)           │
│  ├─ plan.tigimax.mooo.com -> Forward-Auth -> usrv01 :8804 (Plan Analytics)          │
│  ├─ panel.tigimax.mooo.com-> Forward-Auth -> mc-node1 :80 (Wings /api/remote/ bypass)│
│  ├─ tigimax.mooo.com      -> Forward-Auth -> usrv01 :3000 (Planka Kanban)           │
│  └─ Shared Session Cookie: .tigimax.mooo.com (Single Sign-On über alle Portale)      │
└──────────────────────────────────────────┬───────────────────────────────────────────┘
                                           │
         ┌─────────────────────────────────┼─────────────────────────────────┐
         ▼                                 ▼                                 ▼
┌── anode01 (VM 105, .164) ──┐  ┌── idp-01 (VM 121, .150) ───┐  ┌── mc-node1 (VM 119, .148) ─┐
│ OpenLDAP 2.5.20 (LDAPS :636)│  │ Authentik 2026.8.5 (:9000) │  │ Pterodactyl Node 1 / Proxy │
│ ├─ Single Source of Truth  │  │ ├─ Enrollment Flow (2FA)   │  │ ├─ Velocity Proxy 3.5.1    │
│ ├─ wdAccount Schema        │  │ ├─ wd-identity.service:8000│  │ │  (:25565 TCP/UDP) [512M] │
│ ├─ cn=wd_admins, wd_mods   │  │ ├─ wd-bot.service (Discord)│  │ ├─ Decoupled BlueMap :8123 │
│ └─ Proxmox & Homelab Admin │  │ └─ mc-admin-sync.timer(60s)│  │ └─ Panel Web & Wings :8080  │
└────────────────────────────┘  └────────────────────────────┘  └──────────────┬──────────────┘
                                                                               │ Intern :25574
                                                                               ▼
                                                                ┌── mc-main (VM 120, .149) ──┐
                                                                │ Purpur 26.3 (Build 2654)   │
                                                                │ ├─ Java 25 LTS (Container) │
                                                                │ ├─ Heap: 11 GB (Aikar)     │
                                                                │ ├─ Wings Node 2 API :8080  │
                                                                │ ├─ Whitelist & LuckPerms   │
                                                                │ └─ Tectonic + SnowyWorld   │
                                                                └────────────────────────────┘
```

---

## 2. Identity Management & Onboarding (`wd-identity`)

Alle Admin- und Mod-Zugänge werden über das Werkzeug [`wd-admin`](tools/wd-identity/README.md) und den Discord-Bot **WHD Privileges** verwaltet.

### Wichtigste Befehle (direkt im Mac-Terminal):
```bash
# 1. Teammitglied einladen (Minecraft-Name muss NICHT bekannt sein):
wd-admin invite <discord_name> [--role admin|mod]

# 2. Passwort zurücksetzen (sendet Recovery-Link per Discord-DM):
wd-admin reset <discord_name>

# 3. Aktive Teammitglieder und Berechtigungen auflisten:
wd-admin list

# 4. Berechtigungen entziehen (LDAP, Planka, Whitelist, LuckPerms):
wd-admin revoke <discord_name>

# 5. Minecraft Whitelist & LuckPerms manuell synchronisieren:
wd-admin sync
```

### Automatische Onboarding-Kette:
1. Administrator führt `wd-admin invite <discord_name>` aus.
2. Discord-Bot sendet einen persönlichen, 24h gültigen Authentik-Einladungslink per DM.
3. Nutzer setzt im Browser sein Kennwort, richtet verpflichtend **2FA (TOTP)** ein und trägt seinen Minecraft-Namen ein.
4. Backend validiert den Spieler über die Mojang-API, schreibt die UUID ins OpenLDAP, synchronisiert Planka und schaltet Whitelist sowie LuckPerms-Gruppe (`admin`/`mod`) auf dem Purpur-Server frei.
5. Bot sendet dem Nutzer eine Willkommens-DM mit allen Links zu den Portalen.

---

## 3. Repository-Struktur

```
.
├── README.md
├── docs/
│   ├── identity_sso_ldap_discord_guide.md       # Vollständige SSO, LDAP & Discord Dokumentation
│   ├── phase2_pterodactyl_deployment_report.md  # Deployment Report für Pterodactyl & Velocity
│   ├── minecraft_pterodactyl_migration_plan.md  # Migrations- & Tuning-Plan
│   ├── minecraft_plan_critical_review.md        # Kritische Architektur-Prüfung
│   └── minecraft_crafty_guide.md                # Bisherige Crafty Controller Dokumentation
├── tools/
│   └── wd-identity/                             # Identity Management, Webhook & Bot Backend
│       ├── bin/                                 # CLI Wrapper (wd-admin, mc-admin-sync)
│       ├── systemd/                             # Systemd Unit Files (Service, Timer, Bot)
│       ├── tests/                               # Unit-Tests (Sanitization, Injection, Idempotency)
│       ├── wd_identity/                         # Python Quellcode (FastAPI, ldap3, discord.py)
│       └── README.md
├── configs/
│   ├── velocity/                                # Velocity Proxy Konfiguration
│   ├── purpur/                                  # Purpur Server Konfiguration
│   ├── paper/                                   # Paper Anti-Xray & World Settings
│   └── spigot/                                  # Spigot & Bukkit Parameter
├── ansible/
│   ├── inventory.ini
│   └── playbooks/                               # Automatisierungs-Playbooks für Nodes
└── scripts/                                     # Wartungs- und Sync-Skripte
```

---

## 4. Detaillierte Dokumentation & Guides
- [Identity, SSO, LDAP & Discord Architektur-Guide](docs/identity_sso_ldap_discord_guide.md)
- [wd-identity Paket & CLI Handbuch](tools/wd-identity/README.md)
- [Pterodactyl & Velocity Deployment Report](docs/phase2_pterodactyl_deployment_report.md)
- [Server-Tuning & Migrationsplan](docs/minecraft_pterodactyl_migration_plan.md)
