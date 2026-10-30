# Projekt-Status & Vorfall-Log: Weihnachtsdorf 7

**Stand:** 29. Oktober 2026  
**Dokument-Zweck:** Zentrale Dokumentation des aktuellen Systemzustands, offener Aufgaben sowie aufgetretener Vorfälle (Troubleshooting-Protokoll).

---

## 1. Aktuelle Vorfälle & Fehleranalysen

### Vorfall A: "Permission Denied" nach erfolgreicher Authentik-Registrierung (z. B. bei `aboutlace`)
- **Symptom:**  
  Nachdem ein Benutzer (z. B. `aboutlace`) dem Einladungslink gefolgt ist, sein Passwort und 2FA eingerichtet hat und auf interne Portale (wie Planka Kanban unter `https://tigimax.mooo.com`) zugreifen möchte, erschien die Authentik-Fehlerseite mit **"Permission Denied"**.
- **Root Cause:**  
  Authentiks Einladungs-Flow (`invitation-enrollment-flow`) legte den Nutzer initially ohne Gruppenmitgliedschaft an (`groups: []`). Die Access Policies für Planka, Panel und Plan (`wd-staff-only-policy`) forderten Mitgliedschaft in `wd_admins` oder `wd_mods`.
- **Behebung:**  
  `AuthentikClient.add_user_to_group()` wurde in `webhook.py` integriert. Bei Abschluss des Enrollments wird der Account nun sofort programmatisch der Authentik-Gruppe zugewiesen.

---

### Vorfall B: "Username is already taken" bei Einladungsannahme
- **Symptom:**  
  Einladung wird geöffnet, Nutzer versucht seinen Benutzernamen einzugeben, Authentik bricht ab mit *"Username is already taken"*, obwohl der Nutzer sich noch nie zuvor eingeloggt hat.
- **Root Cause:**  
  Wird vor der Registrierung ein LDAP-Sync angestoßen, synchronisiert Authentik den LDAP-Nutzer ohne Passwort in seine interne Datenbank. Der Authentik Prompt-Stage für Einladungen schlägt dann fehl.
- **Behebung:**  
  Veraltete / verwaiste Authentik-Accounts vor dem Generieren des Einladungslinks via `authentik.delete_user(clean_username)` löschen. Erst nach Registrierung synchronisieren.

---

### Vorfall C: Unvollständiger User-Revoke
- **Symptom:**  
  `wd-admin revoke` entfernte Nutzer nur aus LDAP-Gruppen, hinterließ aber aktive Sessions, Accounts in Authentik, Accounts in Planka und Datenbankeinträge.
- **Behebung:**  
  `wd-admin revoke` wurde vollständig überarbeitet:
  1. **OpenLDAP:** Entfernt Mitgliedschaften aus allen Gruppen (`wd_admins`, `wd_mods`, `wd_players`) und löscht optional den gesamten LDAP-Account.
  2. **Authentik IdP:** Löscht Benutzerkonto, entwertet alle aktiven Sessions und bereinigt offene Einladungen.
  3. **Planka Kanban:** Beendet aktive Postgres-Sessions, leert Board-Mitgliedschaften und löscht das Planka-Konto.
  4. **Minecraft Server:** Führt `kick` aus (falls online), entfernt aus Whitelist und löscht LuckPerms-Gruppen/Rechte via RCON/Wings.
  5. **SQLite DB:** Bereinigt offene Bans und archiviert/annulliert offene Bewerbungen.

---

### Vorfall D: Doppelte Slash-Befehle in der Discord Autocomplete-Vorschau
- **Symptom:**  
  Im Discord-Client wurden beim Tippen von `/` sämtliche Befehle des Bots doppelt angezeigt.
- **Root Cause:**  
  `discord_bot.py` synchronisierte die Slash Commands sowohl global (`await tree.sync()`) als auch gildenspezifisch (`await tree.sync(guild=guild)`). Da Discord globale und lokale Befehle separat im Cache hält, entstanden Duplikate.
- **Behebung:**  
  Globale Befehle via Discord REST API gelöscht (`PUT /applications/.../commands` mit `[]`) und in `on_ready()` globalen Sync deaktiviert. Slash-Commands werden nun ausschließlich direkt auf die Guild synchronisiert (sofortige Aktualisierung ohne 1-Stunden-Discord-Cache).

---

### Vorfall E: Rechte-Sicherheit bei Verlassen des Discord-Servers (Server Leave)
- **Anforderung:**  
  Verlässt ein Spieler oder Teammitglied den Discord-Server, soll der Account nicht gelöscht werden, aber alle Berechtigungen müssen sofort entzogen werden. Kehrt der Nutzer zurück, soll Admins ein Reaktivierungs-Prompt angeboten werden.
- **Lösung:**  
  1. **`membership.py` (`revoke_user_privileges`)**: Strippt LDAP-Gruppen, schaltet Authentik inaktiv (`is_active=False`, `is_banned=True`), deaktiviert Planka (`is_active=false`) und entfernt Minecraft-Whitelist.
  2. **`on_member_remove`**: Führt automatischen Rechte-Entzug aus und loggt detailliert nach `#📜admin-logs`.
  3. **`on_member_join`**: Erkennt zurückkehrende Mitglieder anhand der Discord-ID und postet eine interaktive Karte in `#📜admin-logs` mit `[✅ Account reaktivieren]` und `[✖️ Inaktiv lassen]`.
  4. **Self-Service Opt-Out**: Spieler können sich im Kanal `#📝anmeldung` über den Button `[🚪 Abmelden]` oder per Slash-Command `/abmelden` selbstständig abmelden.

---

### Vorfall F: Kanal-Benennung, Emojis & resilienter Channel-Lookup
- **Anforderung:**  
  Eindeutige Benennung des Admin-Prüfkanals und visuelle Angleichung aller Kanäle mit Emojis passend zum Serverstil.
- **Lösung:**  
  1. `#admin-anmeldungen` umbenannt in **`#📋admin-anträge`** (Prüfung von Aufnahmeanträgen AND Entbannungen).
  2. Alle Kanäle unter Kategorie `Weihnachtsdorf 7` mit führenden Emojis versehen:
     `#📝anmeldung`, `#⚖entbannung`, `#📋admin-anträge`, `#✉admin-einladungen`, `#🎛admin-steuerung`, `#📜admin-logs`.
  3. Channel-Lookup-Logik im Bot gehärtet (`any(k in ch.name.lower() ...)`), sodass Emoji-Präfixe den Namensabgleich niemals stören.

---

### Vorfall G: BlueMap Pre-Launch Schranke für registrierte Spieler
- **Symptom:**  
  Spieler, die sich während der Pre-Launch-Phase erfolgreich über Authentik registriert hatten, erhielten sofortigen Zugriff auf die BlueMap Live-Karte unter `https://map.tigimax.mooo.com`. Dies war unerwünscht, da die Weltkarte vor dem offiziellen Projektstart nur für Teammitglieder (Admins/Mods) einsehbar sein soll.
- **Root Cause:**  
  Die Authentik Expression-Policy `wd-bluemap-access-policy` prüfte lediglich, ob der Nutzer in `wd_players` Mitglied war (`return request.user.groups.filter(name__in=[..., 'wd_players']).exists()`), ohne den Projektstatus (`is_launched`) abzufragen.
- **Behebung:**  
  1. **Authentik Expression Policy (`wd-bluemap-access-policy`) aktualisiert:**
     - Teammitglieder (`superadmins`, `wd_admins`, `wd_mods`) haben dauerhaft Zugang (Vorbereitung & Bau).
     - Reguläre Spieler (`wd_players`) erhalten nur dann Zugriff, wenn `attributes__is_launched=True` auf der Gruppe gesetzt ist oder der API-Endpunkt `/api/project/status` `launched: True` meldet.
     - Im Pre-Launch-Status wird Spielern eine informative Meldung angezeigt (*"Weihnachtsdorf 7 befindet sich noch im Pre-Launch. Die BlueMap Live-Karte wird zum offiziellen Projektstart freigeschaltet."*).
  2. **Automatisierte Synchronisation (`AuthentikClient.set_project_launched`):**
     - Bei `/projekt-start` bzw. `wd-admin project launch` wird das Attribut `is_launched: True` auf die Authentik-Gruppe `wd_players` geschrieben und die Whitelist geöffnet.
     - Bei `/projekt-sperren` bzw. `wd-admin project lock` wird `is_launched: False` gesetzt und BlueMap sofort wieder für normale Spieler gesperrt.
  3. **FastAPI Endpunkt hinzugefügt:** `GET /api/project/status` in `webhook.py` liefert den aktuellen Launch- und BlueMap-Status.

---

### Vorfall H: Flexible 2FA-Option für Spieler via Discord-Bot Einmalcode (`/auth-code`)
- **Anforderung:**  
  Normale Spieler empfanden die verpflichtende Einrichtung einer Authenticator-App (TOTP) oft als zu kompliziert. Es sollte für reguläre Spieler eine alternative, unkomplizierte Zwei-Faktor-Authentifizierung über den `WHD Privileges` Discord-Bot geschaffen werden (Anforderung eines 6-stelligen Login-Codes), während Teammitglieder (`admin`, `mod`) aus Sicherheitsgründen weiterhin zwingend eine TOTP-App nutzen müssen.
- **Lösung:**  
  1. **Authentik Enrollment-Flow Dynamik (`invitation-enrollment-flow`):**
     - Neuer Prompt `wd-enrollment-prompt-mfa-method` (Auswahl: Discord-Bot Code vs. Authenticator-App). Teammitgliedern wird dynamisch nur TOTP angeboten.
     - Expression-Policy `wd-totp-setup-gate-policy` an die TOTP-Setup-Stage (Reihenfolge 30) gebunden: Überspringt das Scannen des QR-Codes für Spieler, die den Bot-Code wählen, erzwingt es für Teammitglieder.
  2. **Authentik Native MFA Engine (`StaticDevice` & `StaticToken`):**
     - Bei Onboarding oder Code-Generierung wird für den Spieler ein bestätigtes `StaticDevice` in Authentiks Datenbank angelegt.
     - Die native Stage `default-authentication-mfa-validation` prüft alle konfigurierten Geräte im selben Eingabefeld (`Authenticator-Code`), wodurch Spieler ohne Methoden-Umschaltung den 6-stelligen Code eingeben können.
  3. **Discord Bot (`WHD Privileges`):**
     - Befehle `/auth-code` und Kurzform `/code` implementiert.
     - Interaktiver Button `[🔑 Login-Code]` in `#📝anmeldung` und standalone `AuthCodeButtonView`.
     - **Strikte Sicherheitsbarriere:** Fragt ein Administrator oder Moderator einen Code über den Bot an, wird die Anfrage sofort abgewiesen und auf die verpflichtende TOTP-App verwiesen.
  4. **Automatisierte Testabdeckung:** 10 neue Unittests in `tests/test_mfa_bot.py` sichern Rollenprüfung, Token-Generierung und SQL-Handling ab (Gesamttestsuite: 44 Tests erfolgreich).

---

### Vorfall I: Fehlende Admin-Gruppenberechtigung nach Registrierung (`tigimax_player`)
- **Symptom:**  
  Nach Annahme einer Einladung mit Rolle `admin` fehlten dem Account `tigimax_player` alle administrativen Berechtigungen für geschützte Dienste (Pterodactyl Panel, Planka Kanban, BlueMap).
- **Root Cause:**  
  1. Der Account war in Authentik und OpenLDAP zunächst noch der Gruppe `wd_players` statt `wd_admins` zugeordnet (`groups: ['wd_players']`).
  2. Die Access Policies (`wd-staff-only-policy` und `wd-bluemap-access-policy`) prüfen zwingend die Mitgliedschaft in `wd_admins` bzw. `wd_mods`, wodurch der Zugriff verwehrt wurde.
- **Behebung:**  
  1. `tigimax_player` in Authentik der Gruppe `wd_admins` zugewiesen und aus `wd_players` entfernt. Attribut `role: admin` gesetzt.
  2. In OpenLDAP den DN `uid=tigimax_player,ou=User,dc=ldap-01,dc=net` in `cn=wd_admins,ou=Groups,dc=ldap-01,dc=net` eingetragen und aus `cn=wd_players` entfernt.
  3. Planka- und LuckPerms-Rollen synchronisiert (`role: admin`).
  4. Policy-Checks verifiziert: `wd-staff-only-policy: passing=True`, `wd-bluemap-access-policy: passing=True`.


---

## 2. Infrastruktur- & Komponenten-Übersicht

| Service | Host / IP | Status | Beschreibung |
| :--- | :--- | :--- | :--- |
| **Authentik IdP** | `idp-01` (`192.168.1.150:9000`) | **Aktiv** | Zentrales SSO & Identity Management (`https://auth.tigimax.mooo.com`) |
| **OpenLDAP** | `ldap-01` (`192.168.1.164:636`) | **Aktiv** | Zentraler Verzeichnisdienst für Rollen & Identitäten |
| **Planka Kanban** | `usrv01` (`192.168.1.174:3000`) | **Aktiv** | Staff-Planung (`https://tigimax.mooo.com`) mit Forward-Auth Auto-Login |
| **Pterodactyl Wings** | `mc-node1` (`192.168.1.149:8080`) | **Aktiv** | Minecraft Node 2 Daemon, RCON & Telemetrie-Schnittstelle |
| **BlueMap** | `mc-node1` (`192.168.1.148:8123`) | **Aktiv** | Web-Weltkarte (`https://map.tigimax.mooo.com`), geschützt via `wd-bluemap-access-policy` |
| **WHD Discord Bot** | `idp-01` (`wd-bot.service`) | **Aktiv** | Discord-Bot (`WHD Privileges`) mit 22 Slash Commands & Audit Logging |
| **WD Identity API** | `idp-01` (`wd-identity.service`) | **Aktiv** | Webhook & CLI-Backend (`/opt/wd-identity`), Port 8000 |

---

## 3. Discord-Channels (Kategorie "Weihnachtsdorf 7")

| Kanal | ID | Berechtigung | Funktion |
|---|---|---|---|
| **`#📋admin-anträge`** | `1565033844535722085` | Nur Admins | Prüfung aller Aufnahme- und Entbannungsanträge mit Schnell-Aktionen |
| **`#✉admin-einladungen`** | `1565288748718100552` | Nur Admins | Interaktive Erstellung von Authentik-Einladungen (Admin-Rolle nur für Discord-Admins) |
| **`#🎛admin-steuerung`** | `1565299837522673674` | Nur Admins | Admin-Steuerungszentrale & Index aller 22 Slash-Befehle |
| **`#📜admin-logs`** | `1565288746088402974` | Nur Admins | Vollständiges System- & Sicherheits-Audit-Log (Echtzeit-REST) |
| **`#📝anmeldung`** | `1565033506227355838` | Öffentlich | Aufnahme-Bewerbung (`[📝 Jetzt bewerben]`) & Self-Opt-Out (`[🚪 Abmelden]`) |
| **`#⚖entbannung`** | `1565291901156851722` | Öffentlich | Entbannungs- & Einspruchsantrag (`[⚖️ Einspruch / Entbannung beantragen]`) |
| **`#📝teilnehmerliste`** | `1565283582593998888` | Öffentlich | Live-Board aller registrierten Spieler und Rollen (30s Sync) |

---

## 4. Vollständige Suite der 22 Discord Slash-Befehle

1. `/projekt-start` – Schaltet alle registrierten Spieler auf der Minecraft-Whitelist frei.
2. `/projekt-sperren` – Versetzt den Server in den Wartungs-/Pre-Launch-Modus (Spieler gesperrt, Staff aktiv).
3. `/zugang-entziehen` – Entzieht einem Nutzer alle Zugriffsrechte restlos.
4. `/spieler-bannen` – Bannt einen Spieler auf dem Minecraft-Server und sperrt BlueMap.
5. `/spieler-entbannen` – Hebt Sperren auf Minecraft und BlueMap wieder auf.
6. `/spieler-kick` – Kickt einen Spieler sofort vom Server mit Angabe eines Grundes.
7. `/spieler-zurücksetzen` – Sendet einen Authentik-Passwort-Recoverylink per DM.
8. `/einladung` – Generiert manuell einen Authentik-Registrierungslink per DM.
9. `/einladung-abbrechen` – Entwertet alle offenen Einladungen eines Nutzers.
10. `/rolle-zuweisen` – Ändert die Rolle eines Nutzers (Admin, Moderator, Spieler).
11. `/admin-spieler` – Detaillierte Sicherheits- und Datenbankauskunft über einen Nutzer.
12. `/sync-alle` – Erzwingt sofortigen Abgleich von LDAP, Whitelist und LuckPerms.
13. `/spielerliste-aktualisieren` – Synchronisiert sofort das Live-Board in `#📝teilnehmerliste`.
14. `/live-status` – Echtzeit-Abfrage von CPU, RAM, Festplatte, Uptime und TPS.
15. `/online-spieler` – Zeigt alle aktuell ingame verbundenen Spieler und TPS an.
16. `/wartung-timer` – Startet einen Ingame-Countdown mit Audio-Cues und schaltet danach die Whitelist um.
17. `/wartung-abbrechen` – Bricht einen laufenden Wartungscountdown sofort ab.
18. `/mc-befehl` – Führt einen beliebigen RCON-Befehl direkt auf der Hauptwelt aus.
19. `/admin-rundruf` – Sendet Ingame-Nachrichten (Chat, Titel oder Actionbar) an alle Spieler.
20. `/anmelden` – Öffnet das interaktive Bewerbungsmodal für Spieler.
21. `/abmelden` – Ermöglicht Spielern die selbstständige Abmeldung vom Projekt.
22. `/entbannen` – Öffnet das Einspruchsmodal für abgelehnte oder gesperrte Nutzer.

---

## 5. Verifizierte Test-Fälle

- **Entbannungsantrag #1 (Troll / Ridiculous):**  
  Eingespeist von `xX_ShadowGamer_2013_HD_Xx`. Verifiziert für Ablehnung mit variablem Cooldown Modal (`[❌ Ablehnen & Sperre setzen]`).
- **Entbannungsantrag #2 (Ehrenhaft / Demütig):**  
  Eingespeist von `Maximilian_Von_Ehrenstein`. Verifiziert für Annahme mit automatischem Pardon/Invite (`[✅ Entbannen / Annehmen]`).
- **Audit-Logging:**  
  Beide Anträge und zukünftige Entscheidungen werden lückenlos in `#📜admin-logs` protokolliert.

---

## 6. Hauptnode Härtetest & Benchmark-Ergebnisse (29. Oktober 2026)

Ein umfassender Belastungstest auf `mc-main` (VM 120, i5-9400F 6C/6T, 12GB RAM, NVMe) hat die volle Belastbarkeit für 80–120 Spieler bestätigt:

1. **Hardware-Throughput (Sysbench & Direct I/O):**
   - Single-Core CPU: `526.58 events/s`, `1.90 ms avg` (stabile Single-Thread-Leistung für Tick-Loop).
   - Multi-Core CPU: `3.082.28 events/s` (nahezu lineare 5.85x Skalierung auf 6 physischen Kernen).
   - Speicher-Bandbreite: `23.246,95 MB/s` (DDR4 Dual-Channel).
   - NVMe-Schreibgeschwindigkeit: `424 MB/s` (direkter Flush via `fdatasync`).
2. **Chunk-Vorabgenerierung (Chunky):**
   - Alle **35.721 Chunks** im 3000x3000er Worldborder sind zu **100 % vorab generiert** (Terralith + Terratonic). Keine Echtzeit-Generierungslags beim Launch!
3. **Entity-Stresstest & Belastungsgrenzen:**
   - Idle-Baseline: `0.3 ms MSPT`, `20.00 TPS`, `< 1 % CPU`.
   - 1.000 aktive Mobs: `11.5 ms MSPT`, `20.00 TPS`, `8 % CPU`.
   - 2.000 aktive Mobs: `28.4 ms MSPT`, `20.00 TPS`, `15 % CPU` (Spark-Report: `https://spark.lucko.me/N6OMr3E67S`).
   - Breaking Point: ~ 3.500 Mobs (MSPT > 50 ms, TPS-Drop auf 14.81).
   - Erholung: Sofortige Rückkehr auf `20.00 TPS` und `0.1 ms MSPT` innerhalb von 3 Sekunden.
4. **Java 25 G1GC & Heap:**
   - Young Gen Pausenzeit: `33.7 ms avg` (74 % schneller als 130 ms SLA).
   - Old Gen Full GCs: **0 Kollektionen** (keine Stop-the-World Hänger).
5. **10-Minuten-Dauertest mit 100 echten Spielern (500m Radius & BlueMap Live-Sync):**
   - 100 native Protokoll-Clients (`ServerPlayer`), verteilt auf 20 Cluster im 500-Meter-Radius.
   - Kontinuierliche Bewegung (`position_look`), Arm-Animationen und God-Mode (Creative + Infinite Effects).
   - Live-Synchronisation im 2-Sekunden-Takt auf [`https://map.tigimax.mooo.com`](https://map.tigimax.mooo.com) ohne Geister-Bots.
   - Konstante **20.00 TPS** und unter **1.0 ms MSPT** über die gesamten 10 Minuten.
   - Spark Flamegraph unter Volllast: [`https://spark.lucko.me/LLAV3CekeI`](https://spark.lucko.me/LLAV3CekeI).
   - Details: Siehe [`docs/benchmark_hauptnode_stress_test_report.md`](docs/benchmark_hauptnode_stress_test_report.md).
