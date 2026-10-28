<div align="center">

# 🎄 Minecraft SMP Weihnachtsdorf 7

**Infrastructure-as-Code, Server-Tuning, Identity Management & Operations-Handbücher**  
für das 80–120 Spieler Weihnachts-Event am Spawn.

[![Purpur](https://img.shields.io/badge/Purpur-26.3_Build_2654-a020f0?logo=purpur&logoColor=white)](https://purpurmc.org)
[![Velocity](https://img.shields.io/badge/Velocity-3.5.1_Build_615-1f6feb?logo=velocity&logoColor=white)](https://papermc.io/software/velocity)
[![Java](https://img.shields.io/badge/Java-25_LTS-orange?logo=openjdk&logoColor=white)](https://adoptium.net)
[![Pterodactyl](https://img.shields.io/badge/Pterodactyl-v1.15.2_Panel-0070f3?logo=pterodactyl&logoColor=white)](https://pterodactyl.io)
[![Authentik](https://img.shields.io/badge/SSO-Authentik_2026.8-fd4b2d?logo=authentik&logoColor=white)](https://goauthentik.io)
[![Ansible](https://img.shields.io/badge/IaC-Ansible_Automation-black?logo=ansible&logoColor=white)](ansible/)
[![Pi-hole](https://img.shields.io/badge/DNS-Pi--hole_Split--DNS-red?logo=pihole&logoColor=white)](configs/pihole/custom.list)

</div>

---

## 🌐 Portale & Endpunkte

| Dienst | Externe Adresse | Internes Ziel | Schutz / Authentifizierung |
|---|---|---|---|
| 🎮 **Minecraft Server** | `tigimax.mooo.com:25565` (TCP) | `mc-node1:25565` | Whitelist (Mojang Auth via Velocity HMAC) |
| 🎙️ **Simple VoiceChat** | `tigimax.mooo.com:24454` (UDP) | `mc-node1:24454` | Proximity Chat UDP (Kernel DNAT) |
| 🔐 **Authentik SSO** | https://auth.tigimax.mooo.com | `idp-01:9000` | 2FA (TOTP / WebAuthn) |
| 🗺️ **BlueMap Live-Karte** | https://map.tigimax.mooo.com | `mc-node1:8123` | Forward-Auth (`wd-bluemap`) |
| 📊 **Plan Analytics** | https://plan.tigimax.mooo.com | `mc-main:8804` | Forward-Auth (`wd-plan`) |
| 🦖 **Pterodactyl Panel** | https://panel.tigimax.mooo.com | `mc-node1:80` | Forward-Auth (`wd-panel`) |
| 📋 **Planka Kanban** | https://tigimax.mooo.com | `usrv01:3000` | Forward-Auth (`wd-planka`) |

> 🔑 Ein gemeinsames Session-Cookie auf `.tigimax.mooo.com` sorgt für nahtloses **Single Sign-On** über alle Web-Portale.

---

## 🏗️ Gesamte System- & Netzwerk-Architektur

```mermaid
flowchart TD
    WAN((Internet / WAN))

    subgraph ingress["usrv01 (192.168.1.174) – Ingress, TLS & DNAT"]
        NGX[Nginx Reverse Proxy<br/>Let's Encrypt TLS]
        DNAT["minecraft-portforward.service<br/>Kernel DNAT (25565/TCP & 24454/UDP)"]
        PLANKA[Planka Kanban :3000]
    end

    subgraph dns["ulxc01 (192.168.1.173)"]
        PIHOLE[Pi-hole DNS :53<br/>Split-DNS *.tigimax.mooo.com<br/>Interne .net Zone]
    end

    subgraph idp["idp-01 (VM 121, 192.168.1.150)"]
        AK[Authentik IdP :9000<br/>Enrollment + 2FA]
        WDI[wd-identity :8000]
        BOT[WHD Privileges Discord Bot]
        SYNC[mc-admin-sync.timer 60s]
    end

    subgraph ldap["anode01 (VM 105, 192.168.1.164)"]
        OL[(OpenLDAP 2.5 LDAPS :636<br/>Single Source of Truth)]
    end

    subgraph node1["mc-node1 (VM 119, 192.168.1.148 auf pnode01)"]
        VEL[Velocity Proxy 3.5.1 :25565<br/>VoiceChat Proxy :24454 UDP]
        BM[BlueMap CLI Server :8123]
        PTERO[Pterodactyl Panel v1.15.2 :80<br/>Wings Node 1 Daemon :8080]
        MDB[(MariaDB 10.11 :3306 & Redis)]
    end

    subgraph main["mc-main (VM 120, 192.168.1.149 auf pnode02)"]
        PUR[Purpur 26.3 · Java 25 LTS<br/>11 GB Heap · Aikar Flags<br/>Tectonic + SnowyWorld Datapacks]
        PLAN[Plan Analytics :8804]
        WINGS2[Wings Node 2 Daemon :8080]
    end

    WAN -- "HTTPS (80/443)" --> NGX
    WAN -- "Game Traffic (25565 / 24454)" --> DNAT
    DNAT -- "Kernel Fast-Path" --> VEL
    NGX --> AK & PLANKA & BM & PTERO & PLAN
    AK --> OL
    WDI --> OL
    VEL -- "Modern Forwarding :25574" --> PUR
    SYNC --> PUR
    PLAN --> MDB
    dns -. "Lokale DNS-Auflösung" .-> ingress & node1 & main & idp
```

---

## 👥 Identity & Berechtigungs-Management (`wd-admin`)

Alle Moderatoren- und Administratoren-Berechtigungen werden zentral über [`wd-admin`](tools/wd-identity/README.md) und den Discord-Bot **WHD Privileges** gesteuert.

| Befehl | Wirkung |
|---|---|
| `wd-admin invite <discord> [--role admin\|mod]` | Einladung per Discord-DM (MC-Name muss vorher nicht bekannt sein) |
| `wd-admin reset <discord>` | Einmaliger Passwort-Recovery-Link per Discord-DM |
| `wd-admin list` | Tabellarische Übersicht aller aktiven Teammitglieder, IGNs & UUIDs |
| `wd-admin revoke <discord>` | Sofortiger Entzug in LDAP, Planka, Whitelist und LuckPerms |
| `wd-admin sync [--force]` | Whitelist & LuckPerms manuell abgleichen (idempotent ohne Spam) |

### 🔄 Automatisierter Onboarding-Workflow

```mermaid
sequenceDiagram
    participant A as Admin
    participant B as Discord-Bot (WHD Privileges)
    participant U as Nutzer
    participant K as Authentik IdP
    participant S as wd-identity Backend
    participant M as Purpur Server

    A->>B: wd-admin invite <discord>
    B->>U: DM mit 24h-Einladungslink
    U->>K: Setzt Passwort + 2FA + Minecraft IGN
    K->>S: Enrollment-Webhook Event
    S->>S: Validiert IGN & Mojang-UUID -> Schreibt LDAP & Planka
    S->>M: Setzt Whitelist & LuckPerms (admin/mod) via Wings API
    B->>U: Sendet Willkommens-DM mit allen Portal-Links
```

---

## 🤖 Infrastructure-as-Code (Ansible Automation)

Das Repository enthält automatisierte Ansible-Playbooks zur vollständigen Orchestrierung:

| Playbook | Pfad | Funktion |
|---|---|---|
| 🌐 **Pi-hole DNS** | [`ansible/playbooks/configure_pihole_dns.yml`](ansible/playbooks/configure_pihole_dns.yml) | Verteilt [`configs/pihole/custom.list`](configs/pihole/custom.list), sichert Alt-Configs und validiert DNS |
| 🛡️ **Firewall & Docker** | [`ansible/playbooks/configure_firewall.yml`](ansible/playbooks/configure_firewall.yml) | Richtet UFW & DOCKER-USER Chains ein (Isolierung von Purpur :25574 und BlueMap :8123) |
| 🚀 **Kernel-Tuning** | [`ansible/playbooks/tune_mc_nodes.yml`](ansible/playbooks/tune_mc_nodes.yml) | Deaktiviert Swap, optimiert Socket-Buffer (`rmem_max`/`wmem_max`) und `max_map_count` |
| 🦖 **Panel & DB Setup** | [`ansible/playbooks/setup_pterodactyl_panel.yml`](ansible/playbooks/setup_pterodactyl_panel.yml) | Installiert PHP 8.3, MariaDB, Redis und Pterodactyl Panel auf Node 1 |
| 🪽 **Wings Daemon** | [`ansible/playbooks/setup_wings_node.yml`](ansible/playbooks/setup_wings_node.yml) | Deployed Pterodactyl Wings Daemon und Systemd-Service auf den Nodes |
| 🗺️ **BlueMap Sync** | [`ansible/playbooks/setup_bluemap_sync.yml`](ansible/playbooks/setup_bluemap_sync.yml) | Deployed automatischen BlueMap World-Sync Service & Timer auf Node 1 |

Ausführen aller Playbooks über das zentrale Inventory:
```bash
ansible-playbook -i ansible/inventory.ini ansible/playbooks/<PLAYBOOK>.yml
```

---

## 📁 Repository-Struktur

```
.
├── ansible/                # Inventory & Playbooks für alle Nodes
│   ├── ansible.cfg         # Optimierte Ansible-Konfiguration
│   ├── inventory.ini       # Node-Zuordnungen & SSH-Parameter
│   └── playbooks/          # DNS-, Firewall-, Wings-, Panel- & Tuning-Playbooks
├── configs/                # Konfigurationen aller Dienste
│   ├── bluemap/            # BlueMap Core, Webserver & Webapp Konfiguration
│   ├── mariadb/            # SQL-Schemas (LuckPerms, CoreProtect, Plan)
│   ├── nginx/              # VHosts (auth, panel, map, plan, planka) & Snippets
│   ├── pihole/             # custom.list (Single Source of Truth für DNS)
│   ├── purpur/             # purpur.yml, paper-global.yml, server.properties
│   └── velocity/           # velocity.toml & voicechat-proxy.properties
├── datapacks/              # tectonic.zip · snowyworld.zip (100% Schneelandschaft)
├── docs/                   # Architektur-, Netzwerk-, Betriebshandbücher & Reports
├── scripts/                # Provisionierung, BlueMap-World-Sync (Timer/Service)
└── tools/wd-identity/      # FastAPI-Backend, Discord-Bot, CLI (wd-admin), Tests
```

---

## 📚 Vollständige Dokumentations-Übersicht

| Dokument | Schwerpunkte & Inhalt |
|---|---|
| 📖 **[Admin & Operations Handbook](docs/admin_operations_handbook.md)** | **Tägliche Wartung, Service-Lifecycle, `wd-admin` Referenz, Spark Profiling, Backups & Incident-Runbook** |
| 🌐 **[Network, DNS & Firewall Guide](docs/network_dns_firewall_guide.md)** | **Topologie, Ingress TLS, Kernel DNAT (`minecraft-portforward`), Pi-hole Split-DNS & UFW/Docker Chains** |
| 🔐 **[Identity, SSO, LDAP & Discord Guide](docs/identity_sso_ldap_discord_guide.md)** | **Vollständige Auth-Architektur, OpenLDAP `wdAccount` Schema, Discord Bot & Planka Provisioning** |
| 🛠️ **[wd-identity Handbuch](tools/wd-identity/README.md)** | **Python-Paket, FastAPI Webhooks, CLI-Installation & Unittest-Suite** |
| 🦖 **[Phase 2 Pterodactyl Deployment Report](docs/phase2_pterodactyl_deployment_report.md)** | **Deployment-Status, 20.00 TPS Verifikation & Schnee-Weltgenerierung** |
| 📐 **[Tuning & Migrationsplan](docs/minecraft_pterodactyl_migration_plan.md)** | **Ursprünglicher Migrationsplan, Aikar Flags & 80-Spieler Optimierungen (Abgeschlossen)** |
| 🎯 **[Kritische Architektur-Prüfung](docs/minecraft_plan_critical_review.md)** | **Architektur-Review, Risikoanalysen & Evaluierung der Tuning-Parameter** |
| 📦 **[Crafty Controller (Legacy)](docs/minecraft_crafty_guide.md)** | **Historische Dokumentation für Weihnachtsdorf 5 (Archiviert / Deprecated)** |

<div align="center">

---

Frohe Weihnachten & viel Spaß im Dorf! 🎅⛄

</div>
