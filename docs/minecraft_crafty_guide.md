# Minecraft Server & Crafty Controller Guide

Comprehensive technical documentation for the Minecraft infrastructure, Crafty 4 Controller, server instances, network ingress, and backup strategy.

---

## 1. System Architecture & Resource Allocation

```
+-----------------------------------------------------------------------------------------+
|                                    pnode02 (192.168.1.200)                              |
|                       Intel Core i5-9400F (6C @ 3.9 GHz Turbo) | 16 GB DDR4             |
|                                                                                         |
|  +-------------------------------------------------+  +------------------------------+  |
|  | VM 102: usrv03.net (192.168.1.147)              |  | VM 200: Atlas-Gaming (.139)  |  |
|  | OS: Ubuntu 22.04.4 LTS (Kernel 5.15)            |  | OS: Windows 11 Pro (AtlasOS) |  |
|  | Role: Crafty Controller 4 & Minecraft Servers   |  | Role: Steam/Sunshine Remote  |  |
|  | CPU: 2 vCPUs (pinned to host cores 0-1)         |  | CPU: 5 vCPUs (cores 1-5)     |  |
|  | RAM: 2 GB (Capped for host headroom)            |  | RAM: 9 GB Dedicated          |  |
|  | Storage: 195 GB Root LV + 98 GB Backup Disk     |  | GPU: GTX 1060 Passthrough    |  |
|  | Startup: Order 1, up=30                         |  | Startup: Order 2, up=60      |  |
|  +-------------------------------------------------+  +------------------------------+  |
|                       Host Headroom: 3.4 GB guaranteed DDR4                             |
+-----------------------------------------------------------------------------------------+
```

### Storage Configuration (VM 102)
* **Root Filesystem (`/`):** `/dev/mapper/ubuntu--vg-ubuntu--lv` (195 GB total, ~89 GB used, ~98 GB free).
* **Dedicated Backup Mount:** `/dev/sdb1` (98 GB total, ~93 GB free) mounted at `/home/tigimax/backups/crafty_backups`.

---

## 2. Ingress Networking & Reverse Proxy

All web traffic routes through Nginx on `ubuntu-server-01` (`usrv01.net` / `192.168.1.174`) with Let's Encrypt TLS:

| Public Domain | Internal Destination | Protocol | Purpose |
| :--- | :--- | :---: | :--- |
| **`panel.tigimax.mooo.com`** | `https://usrv03.net:8443` | HTTPS (WebSocket) | Crafty 4 Web Administration Panel |
| **`map.tigimax.mooo.com`** | `http://192.168.1.147:8123` | HTTP | BlueMap / Dynmap Live 3D World Viewer |
| **`plan.tigimax.mooo.com`** | `http://192.168.1.147:8804` | HTTP | Plan Player & Performance Analytics |

### Local DNS Shortcuts (Pi-hole `192.168.1.173`)
* `crafty.net` / `dynmap.net` -> Resolves to `192.168.1.174` (Nginx redirect to HTTPS panels).
* `usrv03.net` / `ubuntu-srv-03.net` -> Direct LAN IP `192.168.1.147`.

### Game & Voice Chat Ports
* **Minecraft Standard Port:** `25565/TCP` (or `25564/TCP` on Weihnachtsdorf 5).
* **Simple Voice Chat Port:** `24454/UDP` (Proximity voice communication).

---

## 3. Crafty 4 Controller Configuration

* **Installation Path:** `/var/opt/minecraft/crafty/crafty-4`
* **Python Virtual Environment:** `/var/opt/minecraft/crafty/venv`
* **Execution Service:** `crafty.service` (systemd unit running as user `crafty`)
  ```bash
  # Check status / logs
  systemctl status crafty.service
  journalctl -u crafty.service -f
  
  # Restart Crafty
  sudo systemctl restart crafty.service
  ```
* **Database & Configs:**
  * Database: `/var/opt/minecraft/crafty/crafty-4/app/config/db/crafty.sqlite`
  * General Config: `/var/opt/minecraft/crafty/crafty-4/app/config/config.json`
* **Java Runtimes Installed on `usrv03`:**
  * OpenJDK 8, 11, 17, 19, and **OpenJDK 21** (`/usr/lib/jvm/java-1.21.0-openjdk-amd64` default).

---

## 4. Minecraft Server Instances (in Crafty)

All server instances reside under `/var/opt/minecraft/crafty/crafty-4/servers/<UUID>/`:

### 1. Weihnachtsdorf 5 (Active Production World)
* **UUID:** `d1edce29-757a-4fd0-806d-6e5d378497b7`
* **Engine:** `purpur-1.21.1.jar` (Purpur Minecraft 1.21.1)
* **Port:** `25564` (in `server.properties`) / `25565` (in Crafty DB)
* **Configured Launch Heap:** `-Xms10000M -Xmx10000M` *(See Gotcha below)*
* **Key Plugins Installed:**
  * **BlueMap (`v5.5`):** Embedded webserver on port `8123` (`plugins/BlueMap/webserver.conf`).
  * **Plan (`v5.6`):** Embedded web analytics on port `8804` (`plugins/Plan/config.yml`).
  * **Simple Voice Chat (`v2.5.25`):** Proximity voice on port `24454/UDP`.
  * **EssentialsX Suite:** Core commands, spawn, anti-build, protection.
  * **Administration & Security:** LuckPerms (v5.4.131), CoreProtect (v22.4), GrimAC (anti-cheat), OpenInv, PlugManX.
  * **Community & Gameplay:** Adventskalender-2.0, MarriageMaster, BreweryX, DiscordSRV, Chunky, QuickShop-Hikari, SmoothTimber, GSit, WorldEdit.

### 2. Futabacho
* **UUID:** `1bbc8dd1-9327-4a68-8af0-1227416bceb8`
* **Engine:** `purpur-1.21.4.jar`
* **Port:** `25565` (Crafty DB port: `25566`)
* **Heap:** `-Xms6000M -Xmx7000M`

### 3. yessir
* **UUID:** `24b72ead-8586-4830-8fb6-873884763441`
* **Engine:** `purpur-1.21.8.jar`
* **Port:** `25565`
* **Heap:** `-Xms4000M -Xmx4000M`

### 4. Weihnachtsdorf 6 prep
* **UUID:** `6a1cabdb-2ad1-4e6e-b5c8-340ec60b832a`
* **Engine:** `purpur-1.21.10.jar`
* **Port:** `25565`
* **Heap:** `-Xms4000M -Xmx4000M`

---

## 5. Historical Builds & World Storage

Historical Minecraft seasons are preserved across the storage cluster:
* **`usrv03` (`192.168.1.147`):**
  * `/home/tigimax/7c905844-4aca-4f0c-a392-de788be951d0`: Contains `Weihnachtsdorf_4.tar.gz` (5.3 GB) and previous server configuration.
  * `/home/tigimax/minecraft_projects`: Fabric 1.20.1 BetterMC server pack and mods.
* **`usrv02` (`192.168.1.137` / 4TB IronWolf HDD under `/home/tigimax/HDD`):**
  * `Minecraft_Weihnachtdorf`
  * `Minecraft_Weihnachtsdorf_2`
  * `Minecraft_Weihnachtsdorf_3` & `Minecraft_Weihnachtsdorf_3_Rollback_30.10.2022`
  * `Minecraft_Sky_Zwei`
  * `minecraft_projects`

---

## 6. Backup Strategy

1. **Crafty Automated Daily Snapshots:**
   * Automated schedule inside Crafty runs nightly at **03:00 AM**.
   * Saves compressed ZIP archives to `/var/opt/minecraft/crafty/crafty-4/backups/<UUID>/`.
   * Weihnachtsdorf 5 archives are ~1.9 GB per snapshot.
2. **Cluster vzdump VM Backup:**
   * Proxmox `vzdump` snapshots VM 102 weekly on Sundays at **02:00 AM** to `/mnt/backup-staging/dump/` (local SSD staging, `keep-last=1`).
   * Synced by `homelab-cloud-sync.sh` to encrypted Google Drive (`gdrive-crypt:vzdump`, `keep-last=3`).
3. **Historical Worlds (Restic):**
   * Nightly Restic deduplication snapshots `/mnt/usrv02-hdd` on `pnode01` to SSD staging and Google Drive (`gdrive-crypt:restic`).
   * Dynmap/BlueMap tile caches (`**/dynmap/**/tiles`, `**/web/tiles`) are explicitly excluded to conserve storage.

---

## 7. Critical Operational Gotchas

> [!WARNING]
> **RAM Allocation vs. Java Heap Space:**
> On 2026-10-25, VM 102 was capped to **2 GB RAM** on `pnode02` to give 9 GB to the Windows 11 Gaming VM (`Atlas-Gaming`) while leaving 3.4 GB safe host headroom.
> Because Weihnachtsdorf 5's execution command specifies `-Xms10000M -Xmx10000M` (10 GB), starting the server while VM 102 is at 2 GB RAM will immediately throw an out-of-memory error:
> `Could not reserve enough space for 10000M object heap`.
> **Solution:** If running Minecraft, either temporarily allocate more RAM in Proxmox (`qm set 102 -memory 10240`), adjust the Crafty execution command heap to match available memory, or coordinate RAM allocation with VM 200.

> [!NOTE]
> **FreeDNS 6-Month Inactivity Expiration:**
> `panel.tigimax.mooo.com`, `map.tigimax.mooo.com`, and `plan.tigimax.mooo.com` are managed via FreeDNS.
> Subdomains expire every 6 months if not refreshed in the web UI at [freedns.afraid.org](https://freedns.afraid.org/). If external access fails, log in and re-save the records.
