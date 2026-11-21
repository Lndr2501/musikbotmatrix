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
| `!rejoin` | Bot verbindet sich neu mit dem Call (hilft, wenn er stumm ist) |
| `!playlist create\|delete <name>` | Playlist anlegen / löschen |
| `!playlist add <name> [Link]` · `remove <name> <nr>` | Titel hinzufügen (ohne Link: laufender Titel) / entfernen |
| `!playlist show <name>` · `list` | Inhalt / alle Playlists |
| `!playlist play <name> [shuffle]` · `enqueue <name>` | abspielen (ersetzt Queue) / hinten anhängen |
| `!help` | Kommandoliste |

**Wer darf steuern?** Nur Leute, die selbst im Call sind (Element Call oder
klassischer Call). `!help`, `!status`, `!queue` und `!playlist list|show`
gehen für alle im Raum. Abschaltbar mit
`CALL_ONLY_CONTROL=false`; `ALLOWED_USERS` gilt zusätzlich.

**Auto-Verlassen:** Ist der Song zu Ende und die Warteschlange leer, verlässt
der Bot sofort den Call (`LEAVE_WHEN_DONE=false` schaltet das ab).
Playlists liegen in der SQLite-DB (nur Links/Titel, keine Audiodateien).

Deutsche Aliase funktionieren auch (`!abspielen`, `!pausieren`, `!fortsetzen`,
`!hilfe`, …). Praefix: `!` (konfigurierbar via `BOT_PREFIXES`).

## Web-Dashboard

`!dashboard` im Chat: Der Bot schickt dir per Direktnachricht einen
persönlichen Link. Einmal öffnen, danach bleibst du eingeloggt (90 Tage,
verlängert sich bei jeder Nutzung). Ein neues `!dashboard` macht den alten Link
ungültig, angemeldete Geräte bleiben drin.

Im Dashboard: Wiedergabe steuern (Play/Pause, Spulen, ±10 s, Lautstärke, Loop,
Video, Autoplay), Call beitreten/verlassen/neu verbinden, Warteschlange sortieren
(ziehen), Suche mit Trefferliste („jetzt“, „als Nächstes“, „hinten anstellen“),
Mediathek (eigene Dateien), Datei vom Gerät abspielen, Playlists, Favoriten,
Statistik und die Fade-Regler (Crossfade-Länge, Einblenden, sanft stoppen,
sofort überblenden).

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
nochmal kommt (Schleife mit leerer Warteschlange).

## Bär-DJ und Moderation

Die Kamera des Bots zeigt einen Bären am DJ-Pult – bei MP3s und Streams statt
des Titelbilds, standardmäßig auch bei YouTube-Titeln statt ihres Videos
(`BAER_OVER_VIDEO=false` zeigt dort wieder das Video).
Takt, Bass und Drops kommen live aus dem Audio, das der Bot gerade ausgibt; eine
gleichmäßige Beat-Uhr rastet auf die Kicks ein. Der Bär spielt vorgerenderte
Tanz-Loops (je 4 Beats, 16 Bilder pro Beat) im Songtempo ab, wechselt am
Taktanfang mit Überblendung die Pose, jubelt bei Drops, trinkt in Ruhephasen
Kaffee und bewegt beim Reden den Mund nach der Stimme. Licht, LEDs und
Plattenteller laufen im Takt mit. Alles läuft auf der CPU (480p/24 fps: ~25 %
eines Kerns, ~470 MB RAM).

**Moderation:** Zwischen zwei Titeln gibt es keinen Crossfade, sondern eine
kurze Pause, in der der Bär redet: was lief, manchmal ein Spruch („Ich hoffe,
euch hat das gefallen“), wer sich den nächsten Titel gewünscht hat, was jetzt
kommt. Die Texte werden zufällig zusammengesetzt und wiederholen sich nicht
gleich. Ist die Warteschlange leer, verabschiedet er sich. Bei `!play` sagt er
den Titel über dem (leiser gedrehten) Intro an. Stimme: Piper
(`de_DE-thorsten-medium`, liegt im Image). „(Official Video)“ & Co. werden
nicht vorgelesen, „Interpret - Titel“ wird aufgeteilt. `!skip`/`!stop` brechen
die Moderation sofort ab.

| Variable | Standard | |
|---|---|---|
| `BAER_ENABLED` | `true` | Bär statt Titelbild |
| `ANSAGE_ENABLED` | `true` | Ansagen/Moderation mit Stimme |
| `MODERATION_ENABLED` | `true` | `false`: wieder Crossfade, nur kurze Ansage über dem Intro |
| `MODERATION_TZ` | `Europe/Zurich` | für „Guten Abend“, „Freitagabend“ |
| `BAER_OVER_VIDEO` | `true` | Bär auch bei Titeln mit eigenem Video (YouTube); `false` zeigt das Video |
| `BAER_ASSETS` | eingebaut | Ordner mit eigenem Bär-Paket (z. B. `/data/baer-qwen`) |

**Eigener Bär (3D):** Eingebaut ist ein einfacher Bär aus Einzelbildern. Ein
richtig animiertes Paket entsteht mit Blender (z. B. auf einem Mac, ~0,25 s pro
Bild, ~15 min für alle Clips):

```sh
blender -b --factory-startup -P tools/baer3d/baer3d.py -- --out clips [--clips mix,wave]
python tools/baer3d/pack.py clips data/baer-paket      # PNG -> WebP, ~16 MB
```

Den Ordner nach `data/` legen (lesbar für UID 10000) und
`BAER_ASSETS=/data/baer-paket` setzen. Ein kaputtes Paket fällt auf den
eingebauten Bären zurück.

## Mediathek (Ordner `radio/`)

MP3s (auch m4a, ogg, opus, flac, wav; Unterordner gehen) in `radio/` neben der
`compose.yaml` legen (oder im Dashboard unter „Mediathek“ → „Zur Mediathek hinzufügen“,
max. 100 MB pro Datei). Im Dashboard-Tab „Mediathek“ lassen sie sich einzeln abspielen,
einreihen oder alle (gemischt) spielen. Der Bot legt dort nur neue Dateien an und löscht oder
überschreibt nie etwas (gleicher Name → „ (2)“). Der Host-Ordner muss für UID 10000
beschreibbar sein (`chown 10000 radio`). Neue Dateien erscheinen sofort;
kein Neustart nötig. `MAX_SONG_SECONDS`
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

Aufbau der Matrix-Anbindung: `matrix.py` setzt auf die Bot-Library
[diddybot](https://github.com/Lndr2501/diddy/tree/main/sdk/python). Die Library erledigt Login, Sync, E2EE, Einladungen,
Entdoppeln der Nachrichten und die Befehlsliste samt Bot-Profil für Diddy; `bot.py` hängt jeden Befehl an die Library und
führt ihn wie bisher aus (`_on_text`). `matrix.py` behält, was nur der Musikbot braucht: Anruf-Signalisierung, Element
Call (To-Device, Geräte, OpenID), angehängte Dateien, Direktnachrichten und Karten. Ende-zu-Ende gegen einen Synapse:
`DIDDY_TEST_HOMESERVER=http://127.0.0.1:8008 pytest tests/test_integration.py`.

Update vom Bot vor Version 0.2: Der alte Token (`/data/matrix-store/access_token`) wird beim ersten Start in die
`credentials.json` der Library übernommen. Gerät und E2EE-Schlüssel bleiben gleich.

Der Mix-Loop (Dekodieren, Lautstaerke, Synchronisation) laeuft genau einmal
unabhaengig von der Peer-Zahl; pro Geraet wird nur enkodiert. Audio ist
immer 48 kHz Stereo (Opus), Video standardmaessig 480p/24fps (VP8).

## Bekannte Grenzen

- Test gegen einen echten Homeserver laeuft erst auf deiner Umgebung (in der
  Entwicklungsumgebung gab es nur Unit- und Loopback-Tests).
- Sehr grosse Gruppen (> ~10 Geraete) brauchen viel Upstream des Bots.
- Das neue Matrix-Call-Produkt (Element Call / LiveKit, "group calls") wird
  bewusst NICHT unterstuetzt – nur klassische Anrufe (Mesh).
