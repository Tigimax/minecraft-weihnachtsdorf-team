# Minecraft SMP Weihnachtsdorf 7

Infrastructure-as-Code, Serverkonfigurationen, Tuning und Migrations-Playbooks für das **Minecraft SMP Weihnachtsdorf 7** (80-Spieler Event am Spawn).

---

## 1. Architektur-Übersicht

```
┌────────────────── Internet ──────────────────┐
│               Port 25565 (TCP)               │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────── pnode01 (.199) ────────────────┐
│  Dedizierte VM / LXC:                          │
│  ├─ Pterodactyl Panel (HTTPS :443)             │
│  ├─ MariaDB 10.11+ & Redis                     │
│  └─ Wings (Docker):                            │
│     └─ Velocity Proxy (:25565) [512 MB]        │
└──────────────────────┬─────────────────────────┘
                       │ Intern LAN :25574
                       ▼
┌──────────────── pnode02 (.200) ────────────────┐
│  Dedizierte VM 120 (mc-main):                  │
│  ├─ 6 Kerne (affinity 0-5), 14 GB RAM          │
│  └─ Purpur Hauptwelt (:25574)                  │
│     ├─ Heap: 11 GB (-Xms11G -Xmx11G)           │
│     ├─ Paper Anti-Xray Engine Mode 2           │
│     ├─ Spark Profiler & Chunky Pre-Gen         │
│     └─ Alternate Current Redstone              │
│  (Atlas Gaming VM 200 gestoppt während Event)  │
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
├── configs/
│   ├── velocity/
│   ├── purpur/
│   ├── paper/
│   └── spigot/
└── scripts/
    ├── backup_world.sh
    └── chunky_pregen.sh
```

---

## 3. Quick-Links & Dokumentation
- [Migrations- & Tuning-Plan](docs/minecraft_pterodactyl_migration_plan.md)
- [Kritische Architekturprüfung](docs/minecraft_plan_critical_review.md)
- [Bisherige Crafty Controller Dokumentation](docs/minecraft_crafty_guide.md)
