# Phase 2 Deployment Report: Pterodactyl Panel, Velocity Proxy, & Weihnachtsdorf 7

**Datum**: 2026-10-27  
**Event**: Weihnachtsdorf 7  

---

## 1. Übersicht & Status

Phase 2 wurde erfolgreich abgeschlossen und verifiziert:

1. **VM 119 (`pterodactyl-node1`, `192.168.1.148` auf `pnode01`)**:
   - **Docker CE 29.9.2** & **Pterodactyl Wings v1.13.4** aktiv.
   - **MariaDB 10.11.14** auf `0.0.0.0:3306` konfiguriert.
     - Datenbanken: `panel`, `mc_luckperms`, `mc_coreprotect`, `mc_plan`.
     - Benutzer: `pterodactyl`@`127.0.0.1` und `minecraft`@`192.168.1.%`.
   - **Redis 7.0.15** aktiv für Cache & Queues.
   - **Pterodactyl Panel v1.15.2** (PHP 8.3-FPM, Composer, Queue-Worker `pteroq.service`, Cron) lokal auf Port 80.
   - **Node 1 (`mc-node1`)** im Panel registriert, Wings verbunden und gesund.

2. **Reverse Proxy auf `usrv01` (`192.168.1.174`)**:
   - Nginx-Vhost für `panel.tigimax.mooo.com` -> `http://192.168.1.148:80`.
   - WebSocket-Forwarding (`Upgrade` und `Connection` Header) aktiv.
   - Let's Encrypt TLS aktiv (`HTTP/2 200 OK`).

3. **VM 120 (`mc-main`, `192.168.1.149` auf `pnode02`)**:
   - **Docker CE 29.9.2** & **Pterodactyl Wings v1.13.4** aktiv.
   - **Node 2 (`mc-main`)** im Panel registriert, Wings verbunden und gesund.

4. **Velocity Proxy auf Node 1**:
   - Jar: Latest Recommended Upstream **Velocity 3.5.1 (Build 615)**.
   - Port: `25565` (TCP/UDP).
   - RAM: 512 MB.
   - Forwarding-Modus: `modern` mit geheimem Token.
   - SLP-Ping verifiziert: `Weihnachtsdorf 7 | Event Server` (Max Players: 80).

5. **Purpur Server auf Node 2 ("Weihnachtsdorf 7")**:
   - Jar: Latest Upstream **Purpur 1.21.4 (Build 2416)** auf Java 21 LTS (`ghcr.io/pterodactyl/yolks:java_21`).
   - Port: `25574` (TCP/UDP).
   - Heap: `11.0 GB` (`-Xms11G -Xmx11G` mit Aikar Flags für 11GB), Container-Limit: 13.3 GB.
   - Forwarding: Modern Forwarding zu Velocity Proxy aktiv.

6. **Spektakuläre 100% Schnee-Weltgenerierung**:
   - **Tectonic 3.0.25 Datapack**: Gewaltige Terrain-Höhen bis Y=320, Steilküsten, tiefe Schluchten und riesige Höhlensysteme.
   - **SnowyWorld 1.2.0 Datapack**: 100% winterliche Biome (`frozen_peaks`, `jagged_peaks`, `snowy_slopes`, `grove`, `ice_spikes`, `snowy_taiga`, `snowy_plains`, `frozen_river`, `frozen_ocean`) über die gesamte Oberwelt.
   - Vanilla Client-Kompatibilität: 100% ohne Client-Mods betretbar.
   - Spark Health: **20.00 TPS**, 0.1 ms Tick-Dauer, 0% CPU-Last.
