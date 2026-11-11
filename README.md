<img src="assets/icon.svg" width="64" height="64" alt="">

# MusikBotMatrix

Ein Musikbot fuer **klassische Matrix-Calls**: du schreibst `!play <Link oder
Suchbegriff>` in den Chat, der Bot laedt den Titel mit yt-dlp herunter und
spielt ihn als Audio+Video in einen normalen Anruf (das Telefon-Icon in
Element Web / Element Phone). Gruppen-Calls laufen als Mesh – jedes Geraet
bekommt seinen eigenen Stream, der Bot hoert alle und sendet allen denselben
Mix. Kein Element Call / LiveKit noetig.

## Schnellstart (Docker)

```bash
cp .env.example .env      # Zugang + Raeume eintragen
docker compose up -d --build
docker compose exec musikbot python -m musikbot --check   # Selbsttest
```

Der Bot braucht einen Matrix-Account auf deinem Homeserver. Einladung in
einen Raum senden (er joined automatisch) oder `MATRIX_ROOMS` setzen.
Dann im Raum: `!play rick astley never gonna give you up`.

**Wichtig fuer Anrufe:** aiortc kann beliebige UDP-Ports verwenden, deshalb
laeuft der Container mit `network_mode: host`. Alternativ statisches TURN
eintragen (`TURN_URIS`/`TURN_USERNAME`/`TURN_PASSWORD`) oder
`TURN_DISCOVERY=true`, wenn der Homeserver (Synapse)
`/voip/turnServer` anbietet.

## Daten: was überlebt und was weg ist

Der Bot schreibt alles (Downloads, Datenbank, Matrix-Gerätedaten) in den
Container-Pfad `/data`, und der liegt in einem **Docker-Volume** namens
`musikbot_data` – bewusst kein Bind-Mount in den Projektordner.

| Aktion | Datenbank + Geraet |
|---|---|
| `docker compose restart`, Reboot, `up -d`, Image-Neubau | **bleiben erhalten** |
| `docker compose stop` / `down` | **bleiben erhalten** (Volume bleibt) |
| `docker compose down -v` | **alles weg** (DB, Gerät) |
| UI „Delete/Down“ + Häkchen **Remove volumes** | **alles weg** |
| `./clean.sh` | **alles weg** inkl. Image, prüft danach nach |

Empfohlen zum vollständigen Entfernen:

```bash
./clean.sh            # Container + Netzwerk + Volume + Image
cd .. && rm -rf musikbotmatrix   # zuletzt Projektordner inkl. .env
```

Nach dem Löschen ist der Bot ein **neues Matrix-Gerät**: alte, verschlüsselte
Chat-Verläufe kann er nicht mehr lesen, und du meldest ihn mit Token/Passwort
neu an. Das ist gewollt so.

In Portainer o. Ä. beim Löschen des Stacks **„Remove volumes“** anhaken – ohne
dieses Häkchen bleibt das Volume `musikbot_data` als Einzelstück stehen
(sieht man mit `docker volume ls`). Aufräumen: `docker volume rm musikbot_data`
oder `docker volume prune`.

(Hinweis: der Schlüssel `name:` in der Compose-Datei braucht Docker Compose
v2.18 oder neuer; er sorgt dafür, dass das Volume immer `musikbot_data` heisst,
unabhängig vom Ordnernamen.)

## Kommandos

| Befehl | Wirkung |
|---|---|
| `!play <Link oder Suche>` | erstes Suchergebnis laden & abspielen |
| `!pause` · `!resume` | pausieren / weiter |
| `!stop` · `!skip` · `!previous` | stoppen / naechster / vorheriger |
| `!queue` · `!clear` | Warteschlange anzeigen / leeren |
| `!status` | was laeuft, wie weit, wie viele Peers |
| `!seek <mm:ss>` · `!volume <0-300>` · `!video on|off` | spulen / lauter / Bild an aus |
| `!shuffle` · `!loop on|off` | mischen / aktueller Titel in Dauerschleife |
| `!joincall` · `!leavecall` | Bot ruft alle im Raum an / haengt auf |
| `!playlist create\|delete <name>` | Playlist anlegen / löschen |
| `!playlist add <name> [Link]` · `remove <name> <nr>` | Titel hinzufügen (ohne Link: laufender Titel) / entfernen |
| `!playlist show <name>` · `list` | Inhalt / alle Playlists |
| `!playlist play <name> [shuffle]` · `enqueue <name>` | abspielen (ersetzt Queue) / hinten anhängen |
| `!24/7` · `!24/7 radio` | zufällig die MP3s aus dem Ordner `radio/` (Dauerschleife, neue Dateien sofort dabei) |
| `!24/7 song [Link]` · `!24/7 playlist <name>` · `!24/7 off` | Dauerschleife, Bot bleibt im Call |
| `!help` | Kommandoliste |

**Wer darf steuern?** Nur Leute, die selbst im Call sind (Element Call oder
klassischer Call). `!help`, `!status`, `!queue`, `!playlist list|show` und
`!24/7 status` gehen für alle im Raum. Abschaltbar mit
`CALL_ONLY_CONTROL=false`; `ALLOWED_USERS` gilt zusätzlich.

**Auto-Verlassen:** Ist der Song zu Ende und die Warteschlange leer, verlässt
der Bot sofort den Call (`LEAVE_WHEN_DONE=false` schaltet das ab). Im 24/7-Modus
bleibt er, auch wenn der Call leer ist; `!stop` oder `!24/7 off` beendet ihn.
Playlists liegen in der SQLite-DB (nur Links/Titel, keine Audiodateien).

Deutsche Aliase funktionieren auch (`!abspielen`, `!pausieren`, `!fortsetzen`,
`!hilfe`, …). Praefix: `!` (konfigurierbar via `BOT_PREFIXES`).

## Web-Dashboard

`!dashboard` im Chat: Der Bot schickt dir per Direktnachricht einen
persönlichen Link. Einmal öffnen, danach bleibst du eingeloggt (90 Tage,
verlängert sich bei jeder Nutzung). Ein neues `!dashboard` macht den alten Link
ungültig, angemeldete Geräte bleiben drin.

Im Dashboard: Wiedergabe steuern (Play/Pause, Spulen, Lautstärke, Loop,
Video), Warteschlange sortieren (ziehen), Suche mit Trefferliste („jetzt“,
„als Nächstes“, „hinten anstellen“), Playlists, Favoriten, 24/7-Radio und die
Fade-Regler (Crossfade-Länge, Einblenden, sanft stoppen, sofort überblenden).

Das Dashboard läuft im Bot auf `WEB_PORT` (Standard 8095). Für den Zugriff von
außen einen Reverse-Proxy oder Cloudflare-Tunnel davor setzen und `PUBLIC_URL`
eintragen.

## Übergänge (Crossfade)

Titel aus der Warteschlange werden weich ineinander übergeblendet – wie bei
Apple Music. Der nächste Titel wird dafür rund 45 Sekunden vorher schon
geladen, damit er pünktlich bereitliegt. Länge per `CROSSFADE_SECONDS`
(Standard 6, `0` = aus; bei kurzen Titeln höchstens ein Drittel der Länge).
`!skip`, `!stop`, Seek und ein neues `!play` blenden nur kurz (0,3 s) aus,
damit es nicht knackt. Kein Crossfade gibt es, wenn derselbe Titel direkt
nochmal kommt (Schleife mit leerer Warteschlange, 24/7 mit einem Song).

## Radio-Ordner

MP3s (auch m4a, ogg, opus, flac, wav; Unterordner gehen) in `radio/` neben der
`compose.yaml` legen, dann im Raum `!24/7 radio`. Der Ordner ist read-only in den
Container gemountet, der Bot löscht dort nie etwas. Neue Dateien werden bei jedem
neuen Durchlauf automatisch mitgenommen; kein Neustart nötig. `MAX_SONG_SECONDS`
gilt für Radio-Dateien nicht. Nach der Änderung einmal `docker compose up -d --build`.

## Speicher-Regeln

**Es werden keine Musikdateien gespeichert.** Jeder Titel wird bei jeder
Wiedergabe frisch heruntergeladen (auch Warteschlange, `!loop`, Wiederholung).
Die Datei liegt nur waehrend des Abspielens in `/data/tmp` und wird danach
geloescht; beim Start wird `/data/tmp` (und altes `cache/`, `library/`) geleert.
Songlimit: Titel ueber `MAX_SONG_SECONDS` (Default 600 = 10 Minuten, `0` = aus) werden abgelehnt.
In der Datenbank bleiben nur Metadaten (Titel, URL, Laenge).

## Element Call (MatrixRTC / LiveKit)

Laeuft im Raum ein Element Call, tritt der Bot bei `!play` automatisch bei
(oder per `!joincall`) und sendet Audio/Video ueber die LiveKit-SFU des
Homeservers. In verschluesselten Raeumen nutzt er per-participant E2EE und
schickt seinen Medien-Schluessel Olm-verschluesselt an alle Call-Geraete.
Der LiveKit-Dienst kommt aus den Call-Mitgliedschaften bzw. der
`.well-known` (`org.matrix.msc4143.rtc_foci`); Override: `LIVEKIT_SERVICE_URL`.
Ist der Call leer, verlaesst der Bot ihn wieder.

## Calls: was “klassisch” hier heisst

Der Bot nutzt reines `m.call.*`-Signaling (Spec v0 und v1) plus WebRTC von
Peer zu Peer – exakt das Protokoll, das Element Web/Telefon hinter dem
Telefon-Icon verwenden, inkl. Verschluesselung (DTLS-SRTP) und
Raum-Verschluesselung (E2EE). Gruppen-Calls sind ein Mesh: Bei einem
Gruppenanruf laedt der Bot automatisch alle Geraete im Raum ein (`MESH_INVITE=auto`).

Hinweis: Da der Bot seine Geraeteschluessel nicht interaktiv verifizieren
laesst, zeigt Element den Call ggf. als “unverifizierter Peer” an. Das ist
erwartbar; die Verbindung ist trotzdem verschluesselt.

## YouTube-Blocking

Falls Downloads fehlschlagen: `YTDLP_PLAYER_CLIENT=tv` (oder `android`) setzen
und/oder ein Cookie-File (Browser-Export, z.B. `cookies.txt`) nach `data/`
legen und `YTDLP_COOKIES_FILE=/data/cookies.txt` setzen.

## Entwicklung

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
python -m pytest tests/ -q
```

Der Mix-Loop (Dekodieren, Lautstaerke, Synchronisation) laeuft genau einmal
unabhaengig von der Peer-Zahl; pro Geraet wird nur enkodiert. Audio ist
immer 48 kHz Stereo (Opus), Video standardmaessig 480p/24fps (VP8).

## Bekannte Grenzen

- Test gegen einen echten Homeserver laeuft erst auf deiner Umgebung (in der
  Entwicklungsumgebung gab es nur Unit- und Loopback-Tests).
- Sehr grosse Gruppen (> ~10 Geraete) brauchen viel Upstream des Bots.
- Das neue Matrix-Call-Produkt (Element Call / LiveKit, "group calls") wird
  bewusst NICHT unterstuetzt – nur klassische Anrufe (Mesh).
