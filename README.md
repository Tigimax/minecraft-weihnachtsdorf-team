<div align="center">

# 🎄 Minecraft SMP Weihnachtsdorf 7

**Infrastructure-as-Code, Server-Tuning, Identity Management & Migrations-Playbooks**
für das 80-Spieler Weihnachts-Event am Spawn.

![Purpur](https://img.shields.io/badge/Purpur-26.3-a020f0)
![Velocity](https://img.shields.io/badge/Velocity-3.5.1-1f6feb)
![Java](https://img.shields.io/badge/Java-25_LTS-orange)
![Authentik](https://img.shields.io/badge/SSO-Authentik_2026.8-fd4b2d)
![Ansible](https://img.shields.io/badge/IaC-Ansible-black)

</div>

---

## 🌐 Portale & Endpunkte

| Dienst | Adresse | Schutz |
|---|---|---|
| 🎮 Minecraft Server | `tigimax.mooo.com:25565` (TCP) | Whitelist |
| 🎙️ Simple VoiceChat | `tigimax.mooo.com:24454` (UDP) | – |
| 🔐 Authentik SSO | https://auth.tigimax.mooo.com | 2FA (TOTP) |
| 🗺️ BlueMap Live-Karte | https://map.tigimax.mooo.com | Forward-Auth |
| 📊 Plan Analytics | https://plan.tigimax.mooo.com | Forward-Auth |
| 🦖 Pterodactyl Panel | https://panel.tigimax.mooo.com | Forward-Auth |
| 📋 Planka Kanban | https://tigimax.mooo.com | Forward-Auth |

> Ein gemeinsames Session-Cookie auf `.tigimax.mooo.com` sorgt für **Single Sign-On** über alle Portale.

---

## 🏗️ Architektur

```mermaid
flowchart TD
    NET((Internet)) --> NGX

    subgraph usrv01["usrv01 (.174) – Nginx Ingress + TLS + Forward-Auth"]
        NGX[Nginx / Let's Encrypt]
        PLAN[Plan Analytics :8804]
        PLANKA[Planka :3000]
    end

    subgraph idp["idp-01 (VM 121, .150)"]
        AK[Authentik :9000<br/>Enrollment + 2FA]
        WDI[wd-identity :8000]
        BOT[wd-bot Discord]
        SYNC[mc-admin-sync.timer 60s]
    end

    subgraph ldap["anode01 (VM 105, .164)"]
        OL[(OpenLDAP 2.5 LDAPS :636<br/>Single Source of Truth)]
    end

    subgraph node1["mc-node1 (VM 119, .148)"]
        VEL[Velocity 3.5.1 :25565]
        BM[BlueMap :8123]
        PTERO[Panel + Wings]
    end

    subgraph main["mc-main (VM 120, .149)"]
        PUR[Purpur 26.3 · Java 25<br/>11 GB Heap · Aikar Flags<br/>Tectonic + SnowyWorld]
    end

    NGX --> AK & PLAN & PLANKA & BM & PTERO
    AK --> OL
    WDI --> OL
    VEL -- intern :25574 --> PUR
    SYNC --> PUR
```

---

## 👥 Identity Management (`wd-admin`)

Alle Admin-/Mod-Zugänge laufen über [`wd-admin`](tools/wd-identity/README.md) und den Discord-Bot **WHD Privileges**.

| Befehl | Wirkung |
|---|---|
| `wd-admin invite <discord> [--role admin\|mod]` | Einladung per Discord-DM (MC-Name nicht nötig) |
| `wd-admin reset <discord>` | Recovery-Link per DM |
| `wd-admin list` | Aktive Teammitglieder & Rechte |
| `wd-admin revoke <discord>` | Entzug in LDAP, Planka, Whitelist, LuckPerms |
| `wd-admin sync` | Whitelist & LuckPerms manuell synchronisieren |

### 🔄 Onboarding-Kette

```mermaid
sequenceDiagram
    participant A as Admin
    participant B as Discord-Bot
    participant U as Nutzer
    participant K as Authentik
    participant S as wd-identity
    A->>B: wd-admin invite <discord>
    B->>U: DM mit 24h-Einladungslink
    U->>K: Passwort + 2FA + MC-Name
    K->>S: Enrollment-Webhook
    S->>S: Mojang-UUID prüfen → LDAP, Planka
    S->>S: Whitelist + LuckPerms (admin/mod)
    B->>U: Willkommens-DM mit Portal-Links
```

---

## 📁 Repository-Struktur

```
.
├── ansible/            # Inventory & Playbooks für alle Nodes
├── configs/            # bluemap · mariadb · nginx · paper · purpur · spigot · velocity
├── datapacks/          # tectonic.zip · snowyworld.zip (Weltgenerierung)
├── docs/               # Architektur-, Deployment- & Migrations-Dokumente
├── scripts/            # provision_vms.sh · sync-mc-world.sh · BlueMap-Sync (service/timer)
└── tools/wd-identity/  # FastAPI-Backend, Discord-Bot, CLI, systemd-Units, Tests
```

---

## 📚 Dokumentation

| Dokument | Inhalt |
|---|---|
| [Identity, SSO, LDAP & Discord](docs/identity_sso_ldap_discord_guide.md) | Vollständige Auth-Architektur |
| [wd-identity Handbuch](tools/wd-identity/README.md) | Paket & CLI |
| [Pterodactyl & Velocity Deployment](docs/phase2_pterodactyl_deployment_report.md) | Phase-2-Report |
| [Tuning & Migrationsplan](docs/minecraft_pterodactyl_migration_plan.md) | Performance & Umzug |
| [Kritische Architektur-Prüfung](docs/minecraft_plan_critical_review.md) | Review & Risiken |
| [Crafty Controller (Legacy)](docs/minecraft_crafty_guide.md) | Alte Setup-Doku |

<div align="center">

---

Frohe Weihnachten & viel Spaß im Dorf! 🎅⛄

</div>
