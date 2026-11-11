# Projekt-Status & Vorfall-Log: Weihnachtsdorf 7

**Stand:** 11. November 2026  
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


### Vorfall J: Discord Slash Command Konsolidierung & Registrierung der MFA-Befehle
- **Symptom:**  
  Obwohl die 2FA-Einmalcode-Generierung für Spieler im Backend und als Button (`wd_auth_code_btn`) voll funktionsfähig war, fehlten die `@tree.command`-Deklaratoren für `/auth-code` und `/code` in `discord_bot.py`. Zudem wichen vereinzelte Befehlsnamen in der Dokumentation von den tatsächlichen Registrierungsnamen ab (z. B. `/ban-spieler` vs. `/spieler-bannen`).
- **Behebung:**  
  1. `@tree.command(name="auth-code")` und Kurzform `@tree.command(name="code")` nativ im Bot registriert und getestet.
  2. Robuste Slash-Command-Aliase für volle Kompatibilität hinzugefügt:
     - `/spieler-bannen` als Alias für `/ban-spieler`
     - `/spieler-entbannen` als Alias für `/pardon-spieler`
     - `/einladung` als Alias für `/einladen`
     - `/spieler-zurücksetzen` als Alias für `/passwort-reset`
     - `/spielerliste-aktualisieren` zur manuellen Schnell-Aktualisierung des `#📝teilnehmerliste`-Boards.
  3. Vollständige Unittest-Suite erweitert (58 Unittests erfolgreich).

---

### Vorfall K: Persistente Browser-Sessions & 2FA-Gerätespeicherung in Authentik
- **Symptom:**  
  Beim Schließen und erneuten Öffnen des Browsers mussten sich Benutzer jedes Mal neu bei Authentik anmelden und erneut einen 2FA-Code eingeben.
- **Root Cause:**  
  1. Alle `UserLoginStage`-Instanzen (`default-authentication-login`, `invitation-enrollment-user-login-invitation-enrollment-flow` etc.) standen auf `session_duration: seconds=0` (reines Session-Cookie, das beim Schließen des Browsers gelöscht wird).
  2. Die MFA-Stage `default-authentication-mfa-validation` stand auf `last_auth_threshold: seconds=0`, wodurch bei jedem Login-Durchlauf erneut 2FA abgefragt wurde.
  3. Die Proxy-/OAuth2-Provider (`wd-bluemap`, `wd-planka`, `wd-panel`, `wd-plan`, `wd-forward-auth` etc.) hatten `access_token_validity: hours=1`.
- **Behebung:**  
  1. `session_duration` und `remember_device` auf allen 5 `UserLoginStage`-Instanzen auf **`days=30`** gesetzt (persistentes Browser-Cookie für 30 Tage).
  2. `last_auth_threshold` auf `default-authentication-mfa-validation` auf **`days=30`** gesetzt (2FA wird auf demselben Browser für 30 Tage gemerkt).
  3. `access_token_validity` auf allen Proxy- und OAuth2-Providern auf **`days=30`** (`refresh_token_validity: days=60`) erhöht.

---

### Vorfall L: Rollen-Management (`/rolle`), Moderator-Differenzierung & Temporäre Baurechte (`/mod-baurecht`)
- **Anforderung:**  
  1. Flexible Rollenverwaltung über Discord-Befehl `/rolle` (bzw. CLI `wd-admin role`) zwischen `admin`, `mod` und `player`.
  2. Strikte Differenzierung der Moderator-Rolle von der Admin-Rolle: Moderatoren sind vorrangig für das Ingame-Troubleshooting zuständig (WorldEdit, CoreProtect, Teleportation, Moderation wie Kick/Ban/Mute). Sie haben **keinen** Zugriff auf interne Admin-Kanäle (`#admin-logs`, `#admin-anträge`), Antragsprüfungen oder Backend-Infrastruktur-Tools (Panel, Planka, Plan).
  3. Pre-Launch-Sperre: Moderatoren haben vor Projektstart standardmäßig weder Whitelist- noch Map-Zugriff.
  4. Temporäre Bauberechtigung: Administratoren können Moderatoren zeitlich begrenzte Bau-Zeitfenster erteilen (z. B. 3 Stunden über `/mod-baurecht <user> 3`), um Vorbereitungen auf dem Server mit WorldEdit durchführen zu können.
- **Behebung:**  
  1. **Rollen-Management CLI & Bot**:
     - `wd-admin role <ziel> <admin|mod|player>` und Discord Slash-Befehl `/rolle <ziel> <rolle>` implementiert.
     - Rollenvalidierung: Die Vergabe von `admin` erfordert Discord-Administratorrechte; die Vergabe von `mod` erfordert eine Discord-Moderator-Rolle.
     - System-Accounts (`tigimax`, `akadmin`) sind unantastbar geschützt.
     - Synchronisation erfasst OpenLDAP (`cn=wd_admins`, `cn=wd_mods`, `cn=wd_players`), Authentik-Attribute & -Gruppen, LuckPerms-Elterngruppen und das Teilnehmer-Board.
  2. **Moderator Ingame-Berechtigungen**:
     - `MinecraftSyncClient.setup_mod_permissions()` konfiguriert die LuckPerms-Gruppe `mod`: Präfix `[Mod]`, `worldedit.*`, `coreprotect.*`, Teleportation (`tp`, `tphere`, `tpa`, `tpahere`), Spieler-Inspektion (`invsee`, `vanish`, `fly`) und Moderation (`kick`, `ban`, `mute`, `warn`).
  3. **Temporäre Baurechte (`/mod-baurecht`)**:
     - Persistente Speicherung aktiver Baurechte in `mod_build_grants.json`.
     - Whitelistet Moderatoren sofort für die gewünschte Stundendauer und entfernt sie nach Ablauf im Pre-Launch-Zustand wieder.
  4. **Moderator-Onboarding**:
     - Spezielle Begrüßungsnachricht (`get_mod_welcome_message`), die Aufgaben transparent erläutert.

---

### Vorfall M: Authentik Application Cache Invalidierung & Bereinigung verwaister Attribute
- **Symptom:**  
  Nach der Herabstufung eines Accounts von `admin` auf `mod` wurden im Authentik-Dashboard (`/if/user/`) weiterhin alle Admin-Icons (Pterodactyl Panel, Planka, Plan) angezeigt. Zudem war die BlueMap Live-Karte für Moderatoren im Pre-Launch aufrufbar.
- **Root Cause:**  
  1. Authentik speichert die Liste erlaubter Anwendungs-Icons für jeden Nutzer in der internen Datenbank-Cache-Tabelle `django_postgres_cache_cacheentry` mit 24 Stunden TTL (`%app_access%`). Bei Rollenänderungen wurde dieser Cache bisher nicht invalidiert.
  2. Auf der Authentik-Gruppe `wd_mods` war noch das Attribut `is_launched: True` aus einem vorherigen Testdurchlauf gesetzt, wodurch die Expression-Policy `wd-bluemap-access-policy` den Pre-Launch-Schutz umging.
- **Behebung:**  
  1. `revoke_proxy_sessions()` in `authentik_client.py` erweitert: Löscht bei jeder Rollen- oder Statusänderung zusätzlich alle `django_postgres_cache_cacheentry`-Einträge mit Schlüssel `%app_access%` des Nutzers.
  2. Verwaistes Attribut `is_launched` von `wd_mods` restlos entfernt.
  3. Expression-Policy `wd-bluemap-access-policy` angepasst: Prüft `is_launched` strikt nur noch auf der Gruppe `wd_players` bzw. über den API-Endpunkt `/api/project/status`.
  4. Bestehende Outpost-Proxy-Sessions bereinigt und `authentik-proxy-1` neu gestartet.

---

### Vorfall N: Telemetrie-Präzisierung im `/live-status` Befehl (Festplatten-Build-Limit & Puffer)
- **Symptom:**  
  Der Discord-Befehl `/live-status` zeigte für die Festplatte lediglich `💾 Festplatte: 2237 MB` an. Dies erweckte den Eindruck, der Server besäße insgesamt nur ~2,2 GB Speicherplatz und hätte keinen Puffer mehr.
- **Root Cause:**  
  Die Telemetrie-Methode `get_server_telemetry()` las lediglich die genutzten Bytes (`disk_bytes`) aus und formatierte diese ohne Gesamtlimit. Das in Pterodactyl konfigurierte Build-Limit (`disk_space: 60000` = 60 GB) und der reale Host-Speicher (64 GB frei auf `/dev/sda1`) wurden nicht dargestellt.
- **Behebung:**  
  1. `mc_sync.py`: Liest nun `disk_limit_mb` und berechnet `disk_percent`.
  2. `discord_bot.py`: Formatiert die Festplatten-Metrik transparent in Gigabyte:  
     `💾 Festplatte: 2.2 GB / 59 GB (56.4 GB frei • 3.7%)`.
  3. Dienst `wd-bot.service` neu gestartet und verifiziert.

---

### Vorfall O: Kryptische ANSI-/Terminal-Symbole & JLine-Artefakte im Discord-Konsolen-Kanal (`clean_console_line`)
- **Symptom:**  
  Im Discord-Konsolen-Kanal (`1570071015730716823`) wurden rohe Terminal-Escape-Sequenzen aus Pterodactyl/JLine übertragen, wie z. B.:
  `[K[14:05:25 INFO]: [37m[[1m[37m[96mL[36mP[0m[37m][0m [36m[97m- [36mWeight: [97mNone[0m`
  `>....[?1h=[?2004h>....`
- **Root Cause:**  
  Die Konsolen-Log-Synchronisation (`console_log_sync_loop`) streamte Rohzeilen ohne Bereinigung von VT100/ANSI-Farbcodes, Carriage-Returns (`\r`) und JLine-Prompt-Prefixen (`>....`), die im Terminal interaktiv überschrieben werden, im Discord-Codeblock jedoch als kryptischer Müll sichtbar blieben.
- **Behebung:**  
  1. Implementierung der Filterfunktion `clean_console_line(line: str) -> str` in `chat_bridge.py`:
     - Entfernt `\r`, Steuerzeichen (`[\x00-\x08\x0b-\x1f\x7f]`), reguläre ANSI-Sequenzen (`\x1b\[...`), OSC-Codes (`\x1b\]...`) und Terminal-Sequenzen mit fehlendem `\x1b` (`\x1b?\[(?:K|\?[0-9;]+[hl]=?|[0-9;]{1,10}m)`).
     - Entfernt JLine-Prompt-Artefakte `(?:>+\.{2,}\s*)+` am Zeilenanfang und Zeilenende.
  2. Aktualisierung von `console_filter(lines)`: Bereinigt jede Zeile, verwirft leere Zeilen (`not cleaned.strip()`) und unterdrückt Bot-Echos sowie Sicherheitsnetz-Meldungen (`no player was found`).
  3. Automatische Bereinigung von 33 fehlerhaften Alt-Nachrichten im Discord-Kanal via Discord Bulk-Delete API.

---

### Vorfall P: `tellraw @a` `No player was found` bei 0 Online-Spielern & fehlendes Konsolen-Logging (`BlueMapLiveBridge.java` v1.1.0)
- **Symptom:**  
  Wenn Discord- oder BlueMap-WebChat-Nachrichten an Minecraft weitergeleitet wurden während 0 Spieler online waren, scheiterte `tellraw @a` mit der Fehlermeldung `System chat: No player was found`. Zudem erschienen `tellraw`-Nachrichten generell nicht im Server-Konsolen-Log.
- **Root Cause:**  
  Der Vanilla Minecraft Server führt `tellraw @a` nur aus, wenn mindestens ein Spieler online ist; andernfalls wird ein Fehlerstatus ausgegeben. Zudem spiegelt Paper `tellraw`-Pakete nicht nativ in die Serverkonsole.
- **Behebung:**  
  1. `@EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)` für `ServerCommandEvent` und `RemoteServerCommandEvent` in `BlueMapLiveBridge.java` hinzugefügt:
     - Erkennt `tellraw @a ` mit Präfixen `[D] `, `[Web] ` oder `[Weihnachtsdorf]`.
     - Extrahiert und unescaped den Plaintext aus allen `"text":"..."`-Feldern und gibt ihn via `Bukkit.getConsoleSender().sendMessage(plainText)` sauber in die Konsole aus (`[14:32:01 INFO]: [D] <Tigimax> Hallo`).
     - Prüft `Bukkit.getOnlinePlayers().isEmpty()`: Wenn niemand online ist, wird `event.setCancelled(true)` aufgerufen. Dadurch führt Minecraft `tellraw @a` gar nicht erst aus und die Fehlermeldung `No player was found` ist vollständig eliminiert.
  2. Plugin `BlueMapLiveBridge.jar` mit JDK 25 im Paper-Container neu gebaut, auf `mc-node2` (`192.168.1.149`) installiert und ins Repository übernommen.

---

## 2. Infrastruktur- & Komponenten-Übersicht

| Service | Host / IP | Status | Beschreibung |
| :--- | :--- | :--- | :--- |
| **Authentik IdP** | `idp-01` (`192.168.1.150:9000`) | **Aktiv** | Zentrales SSO & Identity Management (`https://auth.tigimax.mooo.com`) |
| **OpenLDAP** | `ldap-01` (`192.168.1.164:636`) | **Aktiv** | Zentraler Verzeichnisdienst für Rollen & Identitäten |
| **Planka Kanban** | `usrv01` (`192.168.1.174:3000`) | **Aktiv** | Staff-Planung (`https://tigimax.mooo.com`) mit Forward-Auth Auto-Login |
| **Pterodactyl Wings** | `mc-node1` (`192.168.1.149:8080`) | **Aktiv** | Minecraft Node 2 Daemon, RCON & Telemetrie-Schnittstelle |
| **BlueMap** | `mc-node1` (`192.168.1.148:8123`) | **Aktiv** | Web-Weltkarte (`https://map.tigimax.mooo.com`), geschützt via `wd-bluemap-access-policy` |
| **BlueMapLiveBridge** | `mc-main` (`192.168.1.149`) | **Aktiv** | Paper-Plugin v1.1.0: 2s Spielerpositions-Export, Chat-Events, Vanish-Filter & tellraw Interception |
| **Chat & Console Bridge** | `idp-01` (`wd-identity` / `wd-bot`) | **Aktiv** | Bidirektionale Bridge MC ↔ Discord ↔ BlueMap, dynamisches Channel-Routing & Live-Konsole |
| **WHD Discord Bot** | `idp-01` (`wd-bot.service`) | **Aktiv** | Discord-Bot (`WHD Privileges`) mit 26 Slash Commands, Live-Konsole & Audit Logging |
| **WD Identity API** | `idp-01` (`wd-identity.service`) | **Aktiv** | Webhook & CLI-Backend (`/opt/wd-identity`), Port 8000 |

---

## 3. Discord-Channels (Kategorie "Weihnachtsdorf 7")

| Kanal | ID | Berechtigung | Funktion |
|---|---|---|---|
| **`#📋admin-anträge`** | `1565033844535722085` | Nur Admins | Prüfung aller Aufnahme- und Entbannungsanträge mit Schnell-Aktionen |
| **`#✉admin-einladungen`** | `1565288748718100552` | Nur Admins | Interaktive Erstellung von Authentik-Einladungen (Admin-Rolle nur für Discord-Admins) |
| **`#🎛admin-steuerung`** | `1565299837522673674` | Nur Admins | Admin-Steuerungszentrale & Index aller Slash-Befehle |
| **`#📜admin-logs`** | `1565288746088402974` | Nur Admins | Vollständiges System- & Sicherheits-Audit-Log (Echtzeit-REST) |
| **`#💻ingame-konsole`** | `1570071015730716823` | Nur Admins | Live-Admin-Konsole: 2s Log-Streaming, ANSI-Filterung & direkte Befehlsausführung (mit Blacklist) |
| **`#💬ingame-chat`** | `913851557102112799` | Öffentlich | Offizieller Live-Chat-Kanal (nach `/projekt-start` automatisch aktiv) |
| **`#💬prelaunch-chat`** | `1570031673809043476` | Team / Pre-Launch | Chat-Kanal während der Vorbereitungsphase (vor `/projekt-start`) |
| **`#📝anmeldung`** | `1565033506227355838` | Öffentlich | Aufnahme-Bewerbung (`[📝 Jetzt bewerben]`), 2FA-Code (`[🔑 Login-Code]`) & Self-Opt-Out (`[🚪 Abmelden]`) |
| **`#⚖entbannung`** | `1565291901156851722` | Öffentlich | Entbannungs- & Einspruchsantrag (`[⚖️ Einspruch / Entbannung beantragen]`) |
| **`#📝teilnehmerliste`** | `1565283582593998888` | Öffentlich | Live-Board aller registrierten Spieler und Rollen (30s Sync) |

---

## 4. Vollständige Suite der 26 Discord Slash-Befehle & Aliase

1. `/projekt-start` – Schaltet alle registrierten Spieler auf der Minecraft-Whitelist und BlueMap frei.
2. `/projekt-sperren` – Versetzt den Server in den Wartungs-/Pre-Launch-Modus (Spieler gesperrt, Staff aktiv).
3. `/zugang-entziehen` – Entzieht einem Nutzer alle Zugriffsrechte restlos.
4. `/account-loeschen` – Löscht einen Spieler-Account spurlos und vollständig aus allen Systemen (Purge).
5. `/rolle` (Alias: `/role`) – Ändert die Rolle eines Nutzers (admin, mod, player) systemweit mit Discord-Rollenprüfung.
6. `/mod-baurecht` – Erteilt oder entzieht Moderatoren temporäre Bau- und Serverrechte vor dem Start.
7. `/ban-spieler` (Alias: `/spieler-bannen`) – Bannt einen Spieler auf dem Minecraft-Server und sperrt BlueMap.
8. `/pardon-spieler` (Alias: `/spieler-entbannen`) – Hebt Sperren auf Minecraft und BlueMap wieder auf.
9. `/spieler-kick` – Kickt einen Spieler sofort vom Server mit Angabe eines Grundes.
10. `/passwort-reset` (Alias: `/spieler-zurücksetzen`) – Sendet einen Authentik-Passwort-Recoverylink per DM.
11. `/einladen` (Aliase: `/invite`, `/einladung`) – Generiert manuell einen Authentik-Registrierungslink per DM.
12. `/admin-spieler` – Detaillierte Sicherheits- und Datenbankauskunft über einen Nutzer.
13. `/sync-alle` – Erzwingt sofortigen Abgleich von LDAP, Whitelist, Authentik und LuckPerms.
14. `/spielerliste-aktualisieren` – Synchronisiert sofort das Live-Board in `#📝teilnehmerliste`.
15. `/live-status` – Echtzeit-Abfrage von CPU, RAM, Festplatte, Uptime und TPS.
16. `/online-spieler` – Zeigt alle aktuell ingame verbundenen Spieler und TPS an.
17. `/wartung-timer` – Startet einen Ingame-Countdown mit Audio-Cues und schaltet danach die Whitelist um.
18. `/wartung-abbrechen` – Bricht einen laufenden Wartungscountdown sofort ab.
19. `/mc-befehl` – Führt einen beliebigen RCON-Befehl direkt auf der Hauptwelt aus.
20. `/admin-rundruf` – Sendet Ingame-Nachrichten (Chat, Titel oder Actionbar) an alle Spieler oder Direktnachrichten.
21. `/status` – Zeigt deinen eigenen Account-Status, Rollen und 2FA-Status.
22. `/links` – Übersicht aller geschützten Dienste und Server-Adressen.
23. `/auth-code` (Kurzform: `/code`) – Fordert einen 6-stelligen Authentik-Einmalcode für Spieler an.
24. `/anmelden` – Öffnet das interaktive Bewerbungsmodal für Spieler im Kanal `#📝anmeldung`.
25. `/abmelden` – Ermöglicht Spielern die selbstständige Abmeldung vom Projekt.
26. `/entbannen` – Öffnet das Einspruchsmodal für abgelehnte oder gesperrte Nutzer im Kanal `#⚖entbannung`.

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
