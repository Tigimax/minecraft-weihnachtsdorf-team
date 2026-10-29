#!/usr/bin/env python3
"""
Sets up the complete, professional project planning workspace in Planka for Weihnachtsdorf 7.
Connects via SSH to PostgreSQL inside the planka container on usrv01 (192.168.1.174).
"""

import sys
import subprocess
import json

PLANKA_HOST = "192.168.1.174"
PLANKA_USER = "tigimax"
CONTAINER = "planka-postgres-1"
DB_NAME = "planka"
DB_USER = "planka"

def run_psql(sql):
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        f"{PLANKA_USER}@{PLANKA_HOST}",
        f"sudo docker exec -i {CONTAINER} psql -q -t -A -U {DB_USER} -d {DB_NAME}"
    ]
    res = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"SQL Error: {res.stderr.strip()}", file=sys.stderr)
        raise RuntimeError(res.stderr)
    return res.stdout.strip()

def run_psql_json(sql):
    clean_sql = sql.strip().rstrip(";")
    wrapped_sql = f"SELECT COALESCE(json_agg(t), '[]'::json) FROM ({clean_sql}) t;"
    raw = run_psql(wrapped_sql)
    if not raw:
        return []
    return json.loads(raw)

def extract_id(out):
    for line in out.strip().splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    raise ValueError(f"No numeric ID found in output: {out}")

def main():
    print("=" * 70)
    print("📋 Richte Planka Projektplanung für Weihnachtsdorf 7 ein...")
    print("=" * 70)

    # 1. Fetch Project ID
    projects = run_psql_json("SELECT id, name FROM project WHERE name ILIKE '%Weihnachtsdorf%';")
    if not projects:
        print("Kein Projekt 'Weihnachtsdorf' gefunden, lege an...")
        run_psql("INSERT INTO project (name, created_at, updated_at) VALUES ('Weihnachtsdorf 7', NOW(), NOW());")
        projects = run_psql_json("SELECT id, name FROM project WHERE name ILIKE '%Weihnachtsdorf%';")

    project_id = projects[0]["id"]
    print(f"[*] Projekt: Weihnachtsdorf 7 (ID: {project_id})")

    # 2. Fetch or create Boards
    boards = run_psql_json(f"SELECT id, name FROM board WHERE project_id = {project_id};")
    board_map = {b["name"].lower(): b["id"] for b in boards}

    # Ensure Board 1: Durchführung
    durchfuehrung_id = None
    for k in ["durchführung", "durchfuehrung"]:
        if k in board_map:
            durchfuehrung_id = board_map[k]
            break
    if not durchfuehrung_id:
        out = run_psql(f"INSERT INTO board (project_id, position, name, created_at, updated_at) VALUES ({project_id}, 65536, 'Durchführung', NOW(), NOW()) RETURNING id;")
        durchfuehrung_id = extract_id(out)
    print(f"[*] Board 1 (Sprint & Task-Board): Durchführung (ID: {durchfuehrung_id})")

    # Ensure Board 2: Ideen & Feature-Backlog
    ideen_id = None
    for k in ["ideen", "ideen & feature-backlog"]:
        if k in board_map:
            ideen_id = board_map[k]
            break
    if not ideen_id:
        out = run_psql(f"INSERT INTO board (project_id, position, name, created_at, updated_at) VALUES ({project_id}, 131072, 'Ideen & Feature-Backlog', NOW(), NOW()) RETURNING id;")
        ideen_id = extract_id(out)
    else:
        run_psql(f"UPDATE board SET name = 'Ideen & Feature-Backlog' WHERE id = {ideen_id};")
    print(f"[*] Board 2 (Ideen & Konzepte): Ideen & Feature-Backlog (ID: {ideen_id})")

    # 3. Clean up obsolete dummy lists & cards on both boards
    print("\n[*] Bereinige alte Dummy-Listen und Karten...")
    run_psql(f"""
        DELETE FROM card WHERE board_id IN ({durchfuehrung_id}, {ideen_id}) AND name IN ('yessir', 'test');
        DELETE FROM list WHERE board_id IN ({durchfuehrung_id}, {ideen_id}) AND type = 'active';
    """)

    # 4. Fetch all active users and grant editor memberships
    print("\n[*] Synchronisiere Board-Mitgliedschaften für Team...")
    users = run_psql_json("SELECT id, username FROM user_account WHERE is_deactivated = false;")
    for u in users:
        uid = u["id"]
        uname = u["username"]
        for bid in [durchfuehrung_id, ideen_id]:
            run_psql(f"""
                INSERT INTO board_membership (board_id, user_id, role, created_at, updated_at)
                VALUES ({bid}, {uid}, 'editor', NOW(), NOW())
                ON CONFLICT DO NOTHING;
            """)
        print(f"    ✅ Zugriff für {uname} (ID: {uid}) auf beiden Boards gewährt")

    # 5. Setup Board 1: Durchführung (Lists & Labels)
    print("\n[*] Richte Listen und Labels für 'Durchführung' ein...")
    df_lists_def = [
        ("📥 Backlog & Vorbereitung", "dark-granite", 65536),
        ("⏳ In Arbeit", "pumpkin-orange", 131072),
        ("🔍 Testing & QA", "antique-blue", 196608),
        ("🚀 Bereit für Launch", "bright-moss", 262144),
        ("✅ Erledigt", "turquoise-sea", 327680),
    ]

    df_list_ids = {}
    for lname, lcolor, lpos in df_lists_def:
        out = run_psql(f"""
            INSERT INTO list (board_id, type, name, position, color, created_at, updated_at)
            VALUES ({durchfuehrung_id}, 'active', '{lname}', {lpos}, '{lcolor}', NOW(), NOW())
            RETURNING id;
        """)
        lid = extract_id(out)
        df_list_ids[lname] = lid
        print(f"    Liste: {lname} (ID: {lid})")

    # Setup Labels on Durchführung
    run_psql(f"DELETE FROM label WHERE board_id = {durchfuehrung_id};")
    df_labels_def = [
        ("Prio: Hoch / Blocker", "berry-red", 65536),
        ("Prio: Normal", "pumpkin-orange", 131072),
        ("Infrastruktur & Server", "bright-moss", 196608),
        ("wd-identity & Discord", "lagoon-blue", 262144),
        ("Gameplay & Plugins", "antique-blue", 327680),
        ("Community & Moderation", "light-mud", 393216),
    ]

    df_label_ids = {}
    for lbl_name, lbl_color, lbl_pos in df_labels_def:
        out = run_psql(f"""
            INSERT INTO label (board_id, position, name, color, created_at, updated_at)
            VALUES ({durchfuehrung_id}, {lbl_pos}, '{lbl_name}', '{lbl_color}', NOW(), NOW())
            RETURNING id;
        """)
        lbl_id = extract_id(out)
        df_label_ids[lbl_name] = lbl_id

    # 6. Setup Board 2: Ideen & Feature-Backlog (Lists & Labels)
    print("\n[*] Richte Listen und Labels für 'Ideen & Feature-Backlog' ein...")
    ideen_lists_def = [
        ("💡 Neue Vorschläge", "pink-tulip", 65536),
        ("🎯 In Prüfung", "orange-peel", 131072),
        ("🛠️ Angenommen für spätere Updates", "bright-moss", 196608),
        ("❌ Archiviert / Abgelehnt", "dark-granite", 262144),
    ]

    ideen_list_ids = {}
    for lname, lcolor, lpos in ideen_lists_def:
        out = run_psql(f"""
            INSERT INTO list (board_id, type, name, position, color, created_at, updated_at)
            VALUES ({ideen_id}, 'active', '{lname}', {lpos}, '{lcolor}', NOW(), NOW())
            RETURNING id;
        """)
        lid = extract_id(out)
        ideen_list_ids[lname] = lid
        print(f"    Liste: {lname} (ID: {lid})")

    run_psql(f"DELETE FROM label WHERE board_id = {ideen_id};")
    ideen_labels_def = [
        ("Event-Feature", "berry-red", 65536),
        ("Minispiel", "pumpkin-orange", 131072),
        ("Dekoration & Build", "bright-moss", 196608),
        ("Community", "lagoon-blue", 262144),
    ]

    ideen_label_ids = {}
    for lbl_name, lbl_color, lbl_pos in ideen_labels_def:
        out = run_psql(f"""
            INSERT INTO label (board_id, position, name, color, created_at, updated_at)
            VALUES ({ideen_id}, {lbl_pos}, '{lbl_name}', '{lbl_color}', NOW(), NOW())
            RETURNING id;
        """)
        lbl_id = extract_id(out)
        ideen_label_ids[lbl_name] = lbl_id

    # Helper function to create card with labels & tasks
    def add_card(board_id, list_id, name, desc, position, label_names, tasks):
        clean_name = name.replace("'", "''")
        clean_desc = desc.replace("'", "''")
        sql = f"""
            INSERT INTO card (board_id, list_id, type, position, name, description, comments_total, is_closed, created_at, updated_at, list_changed_at)
            VALUES ({board_id}, {list_id}, 'project', {position}, '{clean_name}', '{clean_desc}', 0, false, NOW(), NOW(), NOW())
            RETURNING id;
        """
        out = run_psql(sql)
        cid = extract_id(out)

        # Labels
        target_labels = df_label_ids if board_id == durchfuehrung_id else ideen_label_ids
        for lname in label_names:
            if lname in target_labels:
                run_psql(f"INSERT INTO card_label (card_id, label_id, created_at, updated_at) VALUES ({cid}, {target_labels[lname]}, NOW(), NOW()) ON CONFLICT DO NOTHING;")

        # Tasks Checklist
        if tasks:
            out_tl = run_psql(f"""
                INSERT INTO task_list (card_id, position, name, show_on_front_of_card, hide_completed_tasks, created_at, updated_at)
                VALUES ({cid}, 65536, 'Checkliste / Abnahmekriterien', true, false, NOW(), NOW())
                RETURNING id;
            """)
            tl_id = extract_id(out_tl)

            t_pos = 65536
            for t_title, t_done in tasks:
                clean_t = t_title.replace("'", "''")
                is_done_str = "true" if t_done else "false"
                run_psql(f"""
                    INSERT INTO task (task_list_id, position, name, is_completed, created_at, updated_at)
                    VALUES ({tl_id}, {t_pos}, '{clean_t}', {is_done_str}, NOW(), NOW());
                """)
                t_pos += 65536
        return cid

    print("\n[*] Erstelle strukturierte Karten für 'Durchführung'...")

    # --- ERLEDIGT ---
    add_card(
        durchfuehrung_id, df_list_ids["✅ Erledigt"],
        "Purpur 26.3 SMP Härtetest & 100-Spieler-Stresstest",
        "Umfassender 10-Minuten-Dauertest mit 100 echten simulierten Spielern über 20 Cluster im 500m-Radius.\n\n"
        "**Ergebnisse:**\n"
        "- 20.00 TPS stabil gehalten\n"
        "- Realer MSPT unter 100 Spielern: 35–42 ms (70–85 % Auslastung)\n"
        "- Spark Flamegraph: https://spark.lucko.me/LLAV3CekeI\n"
        "- Cgroup Headroom kalibriert (-Xms8G -Xmx8G auf 12GB Host zur Vermeidung von Netty OOMs)",
        65536,
        ["Prio: Hoch / Blocker", "Infrastruktur & Server"],
        [
            ("Sysbench Single-Core & Multi-Core Hardware Baseline", True),
            ("NVMe Direct I/O Durchsatz (424 MB/s)", True),
            ("Chunky Worldborder Chunk-Vorabgenerierung (35.721 Chunks)", True),
            ("100-Spieler-Protokoll-Dauertest über 10 Minuten", True),
            ("Docker Cgroup RAM-Headroom Kalibrierung (-Xms8G -Xmx8G)", True)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["✅ Erledigt"],
        "wd-identity Discord Bot & Rollenverwaltung",
        "Zentraler Verwaltungs-Bot mit interaktiven Modals, Slash-Commands und Discord-LDAP-Authentik-Synchronisation.\n\n"
        "**Funktionen:**\n"
        "- Modals für `/anmelden` und `/entbannen`\n"
        "- Automatische Synchronisation nach `#📜spielerliste`\n"
        "- Lückenloses Audit-Logging in `#📜admin-logs`",
        131072,
        ["Prio: Hoch / Blocker", "wd-identity & Discord"],
        [
            ("Interaktives Anmelde-Modal (/anmelden)", True),
            ("Entbannungs-System mit variablem Cooldown (/entbannen)", True),
            ("Spielerliste Embed Auto-Update in #📜spielerliste", True),
            ("Admin-Audit Logging in #📜admin-logs", True)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["✅ Erledigt"],
        "Single Sign-On (Authentik) & OpenLDAP Integration",
        "Zentrales IdP auf VM 121 (`idp-01`) für Planka Kanban, Pterodactyl Panel und Plan Analytics.\n\n"
        "Sichert alle internen Services über Nginx Forward-Auth und Single-Sign-On.",
        196608,
        ["Prio: Normal", "Infrastruktur & Server"],
        [
            ("OpenLDAP Verzeichnisdienst mit Gruppen (wd_admins, wd_mods, wd_players)", True),
            ("Authentik Forward-Auth für Planka (tigimax.mooo.com)", True),
            ("Automatisches Provisioning bei Discord-Rollenvergabe", True)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["✅ Erledigt"],
        "BlueMap Web-Livekarte mit Spieler-Positionen",
        "Hochauflösende 3D-Karte auf https://map.tigimax.mooo.com mit kontinuierlicher Echtzeit-Positionssynchronisation.\n\n"
        "Keine Geister-Bots: Nur tatsächlich online verbundene Spieler werden visualisiert.",
        262144,
        ["Prio: Normal", "Infrastruktur & Server"],
        [
            ("BlueMap CLI Docker Container auf mc-node1 (:8123)", True),
            ("Nginx SSL Reverse Proxy & Dynmap-Shortcuts", True),
            ("Live-Spieler-Koordinaten Export (players.json)", True)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["✅ Erledigt"],
        "CoreProtect v24.1 & MariaDB Anbindung",
        "Lückenloses Block- und Transaktions-Logging zur Griefing-Prävention auf der Hauptwelt.",
        327680,
        ["Prio: Normal", "Gameplay & Plugins"],
        [
            ("MariaDB Container mit mc_coreprotect Schema", True),
            ("CoreProtect Plugin auf Purpur 26.3 konfiguriert", True),
            ("Rollback-Funktionstests im Testareal", True)
        ]
    )

    # --- BEREIT FÜR LAUNCH ---
    add_card(
        durchfuehrung_id, df_list_ids["🚀 Bereit für Launch"],
        "Ingame-Wartungsmodus & Countdown Automation",
        "Automatisierte Steuerung für Event-Starts und Wartungsarbeiten direkt aus Discord.\n\n"
        "Befehle:\n"
        "- `/wartung-timer <Minuten> [Grund]` mit Ingame-Sound & Titeln\n"
        "- `/wartung-starten` (sofortiger Whitelist-Lock & Kick)\n"
        "- `/wartung-abbrechen`",
        65536,
        ["Prio: Normal", "wd-identity & Discord"],
        [
            ("Slash-Command /wartung-timer mit Ingame-Sound", True),
            ("Slash-Command /wartung-starten (sofortiger Whitelist-Lock)", True),
            ("Abbruch-Befehl /wartung-abbrechen", True)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["🚀 Bereit für Launch"],
        "Sicherheits- & Proxy-Härtung (Velocity & Paper)",
        "Netzwerkisolierung und Schutz vor unberechtigten Zugriffen.\n\n"
        "- Velocity Proxy mit `online-mode = true` und `login-ratelimit = 3000`\n"
        "- Purpur Modern Forwarding mit `forwarding.secret`\n"
        "- UFW Firewall: Purpur Port 25574 ausschließlich von Velocity erreichbar",
        131072,
        ["Prio: Hoch / Blocker", "Infrastruktur & Server"],
        [
            ("Velocity online-mode = true & Ratelimit aktiv", True),
            ("Purpur Modern Forwarding Secret validiert", True),
            ("UFW / DOCKER-USER Firewall-Regeln scharf geschaltet", True)
        ]
    )

    # --- TESTING & QA ---
    add_card(
        durchfuehrung_id, df_list_ids["🔍 Testing & QA"],
        "Simple Voice Chat Proximity Audio Verifikation",
        "In-Game 3D-Sprachchat für alle Teilnehmer am Event.\n\n"
        "**Test-Fokus:**\n"
        "- UDP-Port 24454 Erreichbarkeit\n"
        "- Richtige akustische Distanzdämpfung im Terralith-Gebirge\n"
        "- Gruppen-Audio-Qualität bei > 15 Spielern am selben Ort",
        65536,
        ["Prio: Hoch / Blocker", "Gameplay & Plugins"],
        [
            ("VoiceChat Plugin auf Purpur installiert (Port 24454 UDP)", True),
            ("Multiplayer-Distanztest mit Mikrofon-Routing", False),
            ("Fallback-Voice-Channel auf Discord konfigurieren", False)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["🔍 Testing & QA"],
        "Spawn-Schutz & Gast-Rechte Audit",
        "Absicherung des Spawn-Areals vor unberechtigten Eingriffen und Griefing.\n\n"
        "Validierung der LuckPerms-Gruppenberechtigungen (`default` vs. `wd_players`).",
        131072,
        ["Prio: Hoch / Blocker", "Gameplay & Plugins"],
        [
            ("Purpur Spawn-Radius und Grief-Schutz verifizieren", False),
            ("Berechtigungen für neue Spieler testen (Gast vs. Freigeschaltet)", False),
            ("Interaktion mit Türen, Knöpfen und Truhen im Spawn-Dorf validieren", False)
        ]
    )

    # --- IN ARBEIT ---
    add_card(
        durchfuehrung_id, df_list_ids["⏳ In Arbeit"],
        "Spawn-Dorf & Festliche Dekoration",
        "Liebevolle Gestaltung des winterlichen Startdorfs mit Häusern, Marktplatz und Tannenbaum.\n\n"
        "Zentrum des Projekts mit Quest-Tafeln und festlicher Beleuchtung.",
        65536,
        ["Prio: Normal", "Community & Moderation"],
        [
            ("Zentraler Weihnachtsbaum mit Beleuchtung", False),
            ("Marktplatz mit Infotafeln und Wegweisern", False),
            ("Willkommens-Bereich für neu spawnende Spieler", False)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["⏳ In Arbeit"],
        "Offizielles Event-Regelwerk & Welcome Guide",
        "Verständliche Dokumentation der Serverregeln für Chat, Bauen und VoiceChat.\n\n"
        "Muss vor dem offiziellen Launch im Discord und Ingame publiziert werden.",
        131072,
        ["Prio: Hoch / Blocker", "Community & Moderation"],
        [
            ("Regelwerk-Text im Discord #📖regeln finalisieren", False),
            ("Ingame-Buch / /rules Befehl bereitstellen", False),
            ("Staff-Richtlinien für Moderatoren abstimmen", False)
        ]
    )

    # --- BACKLOG ---
    add_card(
        durchfuehrung_id, df_list_ids["📥 Backlog & Vorbereitung"],
        "Launch-Day Generalprobe & Team-Briefing",
        "Gemeinsamer Ablauf-Check mit allen Admins und Moderatoren am Vortag des Launches.",
        65536,
        ["Prio: Hoch / Blocker", "Community & Moderation"],
        [
            ("Notfall-Kommunikationskanal definieren", False),
            ("Whitelist-Countdown live testen", False),
            ("Staff-Berechtigungen und Planka-Zugriff prüfen", False)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["📥 Backlog & Vorbereitung"],
        "Automatisierte Offsite-Backups & Disaster Recovery",
        "Tägliche Sicherung aller Welten, Datenbanken und Konfigurationen auf externen Storage.",
        131072,
        ["Prio: Normal", "Infrastruktur & Server"],
        [
            ("Backup-Cronjob für Minecraft-Welten konfigurieren", False),
            ("MariaDB & PostgreSQL Dump-Skripte verifizieren", False),
            ("Wiederherstellungs-Test auf Test-Instanz durchführen", False)
        ]
    )

    add_card(
        durchfuehrung_id, df_list_ids["📥 Backlog & Vorbereitung"],
        "VIP- & Team-Gruppen in LuckPerms feinjustieren",
        "Farbliche Abstimmung der Präfixe und Tablist-Farben für Admins, Moderatoren und Creator.",
        196608,
        ["Prio: Normal", "Gameplay & Plugins"],
        [
            ("Tablist-Präfixe in LuckPerms konfigurieren", False),
            ("Chat-Formatierung und Prefix-Vergabe testen", False)
        ]
    )

    print("\n[*] Erstelle Ideen & Konzept-Karten für 'Ideen & Feature-Backlog'...")

    add_card(
        ideen_id, ideen_list_ids["💡 Neue Vorschläge"],
        "Adventskalender-Truhen am Spawn",
        "Täglich vom 1. bis 24. Dezember öffnet sich am Spawn eine nummerierte Truhe mit kleinen Überraschungen, Festessen oder kosmetischen Items für aktive Spieler.",
        65536,
        ["Event-Feature", "Dekoration & Build"],
        [
            ("Standorte für 24 Truhen am Marktplatz festlegen", False),
            ("Datapack / Command-Block Logik für tagesbasiertes Öffnen erstellen", False),
            ("Belohnungstabelle ausbalancieren", False)
        ]
    )

    add_card(
        ideen_id, ideen_list_ids["💡 Neue Vorschläge"],
        "Schlitten-Rennen durchs Terralith-Gebirge",
        "Eine Rennstrecke aus blauem Eis durch die Terralith-Berge, auf der Spieler mit Booten gegeneinander antreten können.",
        131072,
        ["Minispiel", "Community"],
        [
            ("Streckenverlauf im Gebirge abstecken", False),
            ("Start- und Ziellinie mit Redstone-Erkennung bauen", False),
            ("Bestzeiten-Scoreboard einrichten", False)
        ]
    )

    add_card(
        ideen_id, ideen_list_ids["🎯 In Prüfung"],
        "Wandernder Weihnachtself (Custom Villager)",
        "Ein seltener Villager, der in verschneiten Wäldern auftaucht und spezielle Deko-Items oder Banner gegen Smaragde tauscht.",
        65536,
        ["Event-Feature", "Gameplay & Plugins"],
        [
            ("Custom Trades definieren", False),
            ("Spawnpunkte in Snowy Biomes festlegen", False)
        ]
    )

    add_card(
        ideen_id, ideen_list_ids["🛠️ Angenommen für spätere Updates"],
        "Geheime Geschenkesuche (Scavenger Hunt)",
        "Versteckte Geschenke und Easter-Eggs im gesamten 3000x3000m Worldborder-Areal. Wer alle findet, erhält eine spezielle Discord-Rolle.",
        65536,
        ["Community", "Event-Feature"],
        [
            ("10 geheime Koordinaten mit Hinweisen vorbereiten", False),
            ("Discord-Bot Integration für Lösungs-Eingabe", False)
        ]
    )

    print("\n" + "=" * 70)
    print("✅ Planka Projektplanung für Weihnachtsdorf 7 erfolgreich eingerichtet!")
    print("   🌐 Erreichbar unter: https://tigimax.mooo.com")
    print("=" * 70)

if __name__ == "__main__":
    main()
