# Minecraft SMP Weihnachtsdorf 7

Infrastructure-as-Code, Serverkonfigurationen, Tuning und Migrations-Playbooks für das **Minecraft SMP Weihnachtsdorf 7** (80-Spieler Event am Spawn).

---

## 1. Architektur-Übersicht

```
┌────────────────── Internet ──────────────────┐
│        Port 25565 (TCP) / 24454 (UDP)        │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────── pnode01 (.199) ────────────────┐
│  Dedizierte VM 119 (pterodactyl-node1, .148):  │
│  ├─ Pterodactyl Panel (Nginx HTTP :80)         │
│  ├─ MariaDB 10.11+ (:3306) & Redis (:6379)     │
│  ├─ Wings Node 1 (:8080 & SFTP :2022)          │
│  │  └─ Velocity Proxy (:25565) [512 MB]        │
│  ├─ Decoupled BlueMap CLI Server (:8123)       │
│  └─ UFW + DOCKER-USER: 25565/24454 open;       │
│     80/8080/8123 usrv01 only; 3306 Node 2 only │
└──────────────────────┬─────────────────────────┘
                       │ Intern LAN :25574
                       ▼
┌──────────────── pnode02 (.200) ────────────────┐
│  Dedizierte VM 120 (mc-main, .149):            │
│  ├─ 6 Kerne (affinity 0-5), 14 GB RAM          │
│  ├─ Wings Node 2 (:8080 & SFTP :2022)          │
│  │  └─ Purpur Hauptwelt (:25574)               │
│  │     ├─ Heap: 11 GB (-Xms11G -Xmx11G)        │
│  │     ├─ Paper Anti-Xray Engine Mode 2        │
│  │     ├─ Spark Profiler & Chunky Pre-Gen      │
│  │     └─ Alternate Current Redstone           │
│  └─ UFW + DOCKER-USER: 25574 Node 1 only,      │
│     8080 usrv01/Node 1 only, SSH LAN/VPN only  │
└────────────────────────────────────────────────┘
                       ▲
                       │
┌──────────────── usrv01 (.174) ─────────────────┐
│  Nginx Ingress & TLS Reverse Proxy:            │
│  ├─ panel.tigimax.mooo.com (:443) -> VM119 :80 │
│  ├─ Wings Node 1 TLS (:8081) -> VM119 :8080    │
│  ├─ Wings Node 2 TLS (:8082) -> VM120 :8080    │
│  │  (WSS console WebSocket upgrade terminated) │
│  └─ map.tigimax.mooo.com (:443) -> VM119 :8123 │
│     (BlueMap decoupled viewer + Basic Auth)    │
└────────────────────────────────────────────────┘
```

---

## 2. Repository-Struktur

```
.
├── README.md
├── docs/
│   ├── minecraft_pterodactyl_migration_plan.md
│   ├── minecraft_plan_critical_review.md
│   └── minecraft_crafty_guide.md
├── ansible/
│   ├── inventory.ini
│   └── playbooks/
│       ├── configure_firewall.yml
│       ├── setup_pterodactyl_panel.yml
│       ├── setup_wings_node.yml
│       └── tune_mc_nodes.yml
├── configs/
│   ├── velocity/
│   ├── purpur/
│   ├── paper/
│   └── spigot/
└── scripts/
    ├── bluemap-world-sync.service
    ├── bluemap-world-sync.timer
    ├── provision_vms.sh
    └── sync-mc-world.sh
```

---

## 3. Quick-Links & Dokumentation
- [Migrations- & Tuning-Plan](docs/minecraft_pterodactyl_migration_plan.md)
- [Kritische Architekturprüfung](docs/minecraft_plan_critical_review.md)
- [Bisherige Crafty Controller Dokumentation](docs/minecraft_crafty_guide.md)
