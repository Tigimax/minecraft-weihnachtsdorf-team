# Waldläufer Rudi (`Waldlaufer_Rudi`) - Förster-Bot

Autonomer, gemütlicher Minecraft-Bot für das **Weihnachtsdorf 7**, der als naturverbundener Dorfbewohner agiert (Slice-of-Life, Försterhütte bauen, Bäume pflanzen, mit Spielern plaudern).

## Komponenten

1. **Bot-Logik & Persona**:
   - `bot.js`: Hauptskript (Anbindung via Mineflayer, autonome Entscheidungen & Gemini API)
   - `persona.md`: Charakter, Sprachstil und Verhaltensregeln
   - `memory.json`: Persistenter Speicher für Ziele, Hütte und Spieler-Interaktionen

2. **Konfiguration (`/etc/default/wd-identity`)**:
   - Benötigt den Gemini API-Key und das Modell:
     ```ini
     GEMINI_API_KEY="your_actual_key"
     GEMINI_MODEL="gemini-2.5-flash"
     ```

3. **Systemd-Service (`wd-foerster.service`)**:
   - Steuert den automatischen Start des Förster-Bots:
     ```bash
     sudo cp wd-foerster.service /etc/systemd/system/wd-foerster.service
     sudo systemctl daemon-reload
     sudo systemctl enable --now wd-foerster
     ```

4. **Velocity-Auth Integration (`WDLocalBotAuth.jar`)**:
   - Spezielles Plugin für den Velocity-Proxy, damit lokale Bots ohne interaktive Authentifizierung sicher auf den Server verbunden werden können.
