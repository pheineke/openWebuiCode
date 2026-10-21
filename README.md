# EDU-KI Code

Desktop-App für die KI-Chats der Hochschulen in Rheinland-Pfalz (Open WebUI hinter Shibboleth-Login).
Chat mit Modellauswahl, synchron mit deinen Open-WebUI-Chats, dazu ein Agent, der in einem Projektordner
Dateien lesen und ändern und Befehle ausführen kann (jeweils mit Rückfrage), mit Datei-Ansicht, HTML-Vorschau
und Terminal. Zusätzlich ein lokaler OpenAI-kompatibler Proxy, den z. B. [opencode](https://opencode.ai) nutzen kann.

Inoffiziell: kein Angebot der Hochschulen. Die Nutzungsbedingungen des Dienstes gelten weiter.

## Voraussetzungen

- Linux mit Python 3 (getestet mit 3.14; ab 3.10 sollte es reichen)
- Für das App-Fenster: GTK 4, libadwaita, WebKitGTK 6 und PyGObject.
  Fedora 43 (diese Pakete sind dort installiert): `sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0`
  Fehlen sie, öffnet die App stattdessen ein Chromium-Fenster (dafür `chromium`/`google-chrome` oder
  `.venv/bin/playwright install chromium`).

## Einrichten

```bash
git clone git@github.com:pheineke/openWebuiCode.git
cd openWebuiCode
python3 -m venv --system-site-packages .venv   # --system-site-packages, damit PyGObject sichtbar ist
.venv/bin/pip install -r requirements.txt
```

## Starten

```bash
.venv/bin/python -m edu_ki_proxy app
```

Beim ersten Start siehst du den Anmeldebildschirm. **Mit Hochschul-Login anmelden** lädt die Anmeldung im
App-Fenster: Hochschule wählen, einloggen, die App übernimmt die Sitzung und springt zurück. Dein Passwort
bekommt nur deine Hochschule zu sehen. Klappt das nicht, hilft der Link darunter
(„Stattdessen im Chromium-Fenster anmelden“).

Läuft die Sitzung ab, erscheint der Anmeldebildschirm wieder. Abmelden: Profil unten links → Abmelden.

## Benutzen

- **Ordner** (Chip über dem Eingabefeld): Arbeitsordner wählen. Dann darf das Modell dort lesen, ändern und
  Befehle ausführen. Schreiben und Befehle fragen nach, außer du stellst „Auto“ ein.
- Oben rechts: **Terminal** und **Dateien** (Code mit Syntaxfarben, bei HTML per Auge-Symbol Vorschau).
- **Strg+K** durchsucht Chats, **Strg+,** öffnet die Einstellungen (Darstellung, Nutzung).

## Mit opencode

Solange die App läuft, ist der Proxy unter `http://127.0.0.1:8787/v1` erreichbar. Ohne App:
`.venv/bin/python -m edu_ki_proxy serve` (Port mit `--port`, dann auch die `baseURL` unten anpassen).
In einer `opencode.json` im Projektordner den Proxy als Anbieter eintragen (Modelle mit `python -m edu_ki_proxy models` auflisten):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "eduki": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "EDU-KI-RLP",
      "options": { "baseURL": "http://127.0.0.1:8787/v1", "apiKey": "unused" },
      "models": { "qwen3.5-397b": { "name": "Qwen 3.5 397B" } }
    }
  }
}
```

```bash
opencode -m eduki/qwen3.5-397b
```

Weitere Befehle: `login` (Anmeldung im Chromium-Fenster), `models` (verfügbare Modelle), `tooltest <modell>`
(prüft, ob ein Modell Werkzeugaufrufe kann).

## Wo liegen meine Daten?

`~/.config/edu-ki-proxy/` (anderer Ort: Umgebungsvariable `EDU_KI_HOME`). Dort liegen die Sitzung
(`state.json`, nur für dich lesbar), die Fenster-Daten mit Einstellungen und das Nutzungsprotokoll.
Anmeldedaten löschen: in der App abmelden (entfernt auch das Hochschul-Cookie im Fenster; nur `state.json`
zu löschen reicht nicht, die Hochschule könnte dich sonst ohne Passwort wieder anmelden).
Andere Instanz: `EDU_KI_BASE=https://…`. Anderer Browser für den Chromium-Login: `EDU_KI_BROWSER=/pfad`.

## Fehlersuche

- **Port 8787 belegt** (App startet nicht): `fuser -k 8787/tcp`, dann neu starten.
- **Fenster bleibt leer / 403**: Die App akzeptiert nur Anfragen von `127.0.0.1:<Port>`. Nicht über eine andere
  Adresse oder einen Proxy aufrufen.
- **Modell antwortet nicht mit Werkzeugen**: nicht alle Modelle können das. `qwen3.5-397b` und
  `deepseek-v4-flash` wurden getestet.

## Sicherheit in Kürze

Der Server hört nur auf `127.0.0.1` und prüft Host und Origin, damit andere Webseiten ihn nicht nutzen können
(auch für das Terminal). Die Dateiwerkzeuge bleiben im gewählten Ordner, `bash` ist **nicht** abgeschottet:
Die Rückfrage ist der Schutz, also nur freigeben, was du verstehst. Die HTML-Vorschau läuft in einer Sandbox
ohne Zugriff auf die App.

Gebündelte Bibliotheken (`edu_ki_proxy/static/vendor/`): highlight.js (BSD-3-Clause), xterm.js (MIT).
