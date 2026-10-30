# Weihnachtsdorf 7 Identity System (`tools/wd-identity`) — Multi-Agent Handoff & Status Report

**Stand**: 2026-10-30T17:28:00+01:00  
**Repository Branch**: `main`  
**Letzter Commit**: `6e6b030` (*feat(identity): harden webhook auth, identity resolution, external sync, and test suite*)  
**Test-Status**: ✅ **84 von 84 Tests erfolgreich** (`84 passed in 1.91s`)

---

## 1. Executive Summary (Was wurde erreicht)

Das gesamte Identity-System für Weihnachtsdorf 7 (`tools/wd-identity`) wurde einer umfassenden Sicherheitsanalyse und Härtung unterzogen. Alle 7 Kernanforderungen und 13 Teil-Features wurden implementiert, mit Regressionstests abgesichert und committet.

Die parallelen Explorer- und Worker-Agenten haben vor ihrem Stoppen den genauen Architektur- und Schwachstellen-Katalog erarbeitet. Dieser Bericht bündelt alle Erkenntnisse, damit ein nachfolgender Agent oder Entwickler auf jedem beliebigen Gerät sofort nahtlos anknüpfen kann.

---

## 2. Detaillierter Feature- und Meilenstein-Status (13 Features)

| # | Feature | Ziel | Status | Implementierungsort |
|---|---------|------|--------|---------------------|
| **1** | **Webhook Auth Fail-Closed (R1)** | Authentifizierung per Header `X-Webhook-Secret` oder `Authorization: Bearer <secret>` mit `hmac.compare_digest`. | ✅ Umgesetzt | `wd_identity/webhook.py:verify_webhook_auth` |
| **2** | **Webhook Bind & Exposure (R1)** | Standard-Bind auf `127.0.0.1` statt `0.0.0.0:8000` im Code und systemd. | ✅ Umgesetzt | `config.py:117`, `wd-identity.service:11` |
| **3** | **Account Takeover Protection (R1)** | Verhindern von Passwort-Resets bestehender LDAP-Konten via Onboarding-Webhook. | ✅ Umgesetzt | `webhook.py:process_onboarding` |
| **4** | **Role Escalation Prevention (R1)** | Rollen `admin`/`mod` nur bei Vorautorisierung (bestehender LDAP-Eintrag oder Authentik-Einladung). | ✅ Umgesetzt | `webhook.py:process_onboarding` |
| **5** | **Discord Snowflake Collision (R1)** | Zuweisung einer Discord-ID wird abgewiesen, wenn sie bereits einem anderen LDAP-Konto gehört. | ✅ Umgesetzt | `webhook.py:process_onboarding` |
| **6** | **MFA Auto-Dispatch Hardening (R1)** | Static MFA-Token werden nur an verifizierte Discord-IDs gesendet (kein Angreifer-Spoofing via Request-Body). | ✅ Umgesetzt | `webhook.py:mfa_auto_dispatch` |
| **7** | **Strict Identity Resolution (R2)** | Sämtliche Fallbacks auf veränderbare Discord-Namen (`discordName`) in Bot & Webhook entfernt. | ✅ Umgesetzt | `discord_bot.py`, `utils.py` |
| **8** | **Production Code Cleanliness (R2, R7)** | Alle `MagicMock`-Reflections aus `discord_bot.py` entfernt und Tests sauber isoliert. | ✅ Umgesetzt | `discord_bot.py`, `test_mfa_bot.py` |
| **9** | **LDAP TLS Validation (R3)** | Standardmäßig `ssl.CERT_REQUIRED` mit konfigurierbarer CA-Datei (`LDAP_CA_CERT_FILE`). | ✅ Umgesetzt | `ldap_client.py:59-67` |
| **10** | **Discord Bot Admin Check (R6)** | Nur noch echte Discord-Administratoren oder `DISCORD_ADMIN_ROLE_ID` werden akzeptiert. | ✅ Umgesetzt | `discord_bot.py:is_admin_user` |
| **11** | **Authentik Group Sync (R4)** | Saubere Bereinigung alter Gruppen (`wd_admins`, `wd_mods`) bei Rollen-Downgrades via `remove_user_from_group`. | ✅ Umgesetzt | `authentik_client.py`, `webhook.py` |
| **12** | **Planka SQL Overwrite Protection (R5)** | Priorisiertes Matching nach Username; Schutz gegen PL/pgSQL Dollar-Quote-Injection via `DO $planka_sync_user$`. | ✅ Umgesetzt | `planka_sync.py` |
| **13** | **Full Test Suite & Adversarial Tests (R7)** | Vollständige Testabdeckung in `tests/test_security_hardening.py` für alle Angriffsvektoren. | ✅ Umgesetzt | `tests/test_security_hardening.py` |

---

## 3. Wichtige Erkenntnisse der Explorer-Agenten

### Explorer 1 (Webhook Security):
* **Warnung bei fehlendem Secret**: Wenn in `/etc/default/wd-identity` kein `WEBHOOK_SECRET` gesetzt ist, loggt der Server eine deutliche Sicherheitswarnung.
* **Empfehlung für Produktion**: Bei Bedarf kann `docs_url=None` in `FastAPI(...)` gesetzt werden, falls auch auf dem lokalen Loopback keine Swagger-Dokumentation erreichbar sein soll.

### Explorer 2 (Identity & Bot):
* **Discord Snowflake Striktheit**: Sämtliche Abfragen in `handle_admin_appeal_accept`, `handle_auth_code_request`, `cmd_password_reset` und `cmd_spieler_info` laufen jetzt ausnahmslos über `ldap.search_by_discord_id(numeric_id)`.
* **Bereinigung von Test-Artefakten**: Früher vorhandene `if type(user).__name__ == "MagicMock"`-Hilfskonstrukte im Produktivcode wurden vollständig beseitigt.

### Explorer 3 (External Sync & Tests):
* **Authentik API Sync**: `AuthentikClient.sync_user_groups(username, target_role)` entfernt alte privilegierte Gruppen, bevor die neue Rolle zugewiesen wird. Interne geschützte Konten (`tigimax`, `akadmin`) sind zusätzlich geschützt.
* **Planka SQL**: Bei `sync_user` wird jetzt primär nach `username` gesucht, um zu verhindern, dass ein Nutzer mit einer fremden E-Mail einen anderen Account überschreibt.

---

## 4. Befehle zum Fortsetzen auf einem anderen Gerät

### Testsuite lokal ausführen:
```bash
cd tools/wd-identity
# Alle 84 Tests ausführen
uv run --with pytest pytest -v

# Spezifische Sicherheits-Testsuite ausführen
uv run --with pytest pytest tests/test_security_hardening.py -v
```

### Git-Historie und Änderungen ansehen:
```bash
git log -1 --stat
git show 6e6b030
```

---

## 5. Deployment- & Produktions-Checkliste (Server-Rollout)

Wenn das System auf den Produktionsserver (`usrv01` / Zielsystem) ausgerollt wird:

1. **Konfiguration `/etc/default/wd-identity` aktualisieren**:
   ```bash
   # Sicheres Webhook-Secret generieren und eintragen
   WEBHOOK_SECRET=HIER_EIN_LANGES_ZUFALLS_TOKEN_EINTRAGEN
   WEBHOOK_HOST=127.0.0.1
   WEBHOOK_PORT=8000
   
   # LDAP-Zertifikatsprüfung (falls internes Zertifikat verwendet wird)
   LDAP_TLS_VERIFY=true
   LDAP_CA_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
   
   # Discord Admin Role ID
   DISCORD_ADMIN_ROLE_ID=909437200125538371
   ```

2. **Dienste neu starten**:
   ```bash
   sudo systemctl restart wd-identity.service
   sudo systemctl restart wd-bot.service
   sudo systemctl status wd-identity.service
   ```

3. **Authentik Webhook-Stage konfigurieren**:
   * Im Authentik Admin-Interface unter dem Webhook- bzw. Onboarding-Stage einen benutzerdefinierten HTTP-Header hinterlegen:
     * Header-Name: `X-Webhook-Secret`
     * Header-Wert: `<dein_konfiguriertes_WEBHOOK_SECRET>`
