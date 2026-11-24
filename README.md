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
| `!blick [mich\|Name]` | DJ schaut **jetzt** auf alle Kameras/Bildschirme (oder nur deine / die einer Person) und sagt was dazu |
| `!blick an\|aus\|status` | Kamera-Sprüche nur für dich ab-/anschalten |
| `!geburtstag <Name>` · `aus` | DJ feiert mit: Geburtstags-Outfit/-Bühne (falls im Paket) und ein Ständchen; `!geburtstag` zeigt den Stand |
| `!sticker [Name]` | Sticker schicken (ohne Name: alle zeigen), geht in jedem Raum |
| `!help` | Kommandoliste |

**Wer darf steuern?** Nur Leute, die selbst im Call sind (Element Call oder
klassischer Call). `!help`, `!status`, `!queue`, `!playlist list|show`, `!sticker`
und `!geburtstag` (nur nachsehen) gehen für alle im Raum. Abschaltbar mit
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

**Zurufe und Publikum:** Mitten im Song ruft der Bär ab und zu etwas rein, oft an
jemanden aus dem Call („Lndr, du bist geil!“), nach einem Drop „Wooo! Was für ein
Drop!“. Wer im Call ist, schwebt als runder Avatar im Hintergrund und hüpft im Takt.
Die LED-Wand hinter dem Bären zeigt einen Equalizer aus dem laufenden Audio.

| Variable | Standard | |
|---|---|---|
| `BAER_ENABLED` | `true` | Bär statt Titelbild |
| `ANSAGE_ENABLED` | `true` | Ansagen/Moderation mit Stimme |
| `MODERATION_ENABLED` | `true` | `false`: wieder Crossfade, nur kurze Ansage über dem Intro |
| `MODERATION_TZ` | `Europe/Zurich` | für „Guten Abend“, „Freitagabend“ |
| `DJ_NAME` | `DJ Bär` | Name in Sprechblase und Moderation |
| `DJ_NAME_SPOKEN` | – | so spricht die Stimme den Namen (z. B. `DJ Quenn` für „DJ Kwen“) |
| `BAER_OVER_VIDEO` | `true` | Bär auch bei Titeln mit eigenem Video (YouTube); `false` zeigt das Video |
| `ZURUF_ENABLED` | `true` | Zurufe mitten im Song („Lndr, du bist geil!“, „Hände hoch!“, nach Drops) |
| `ZURUF_EVERY` | `75` | Abstand der Zurufe im Schnitt (Sekunden, zufällig ±40 %) |
| `BAER_ASSETS` | eingebaut | Ordner mit eigenem Bär-Paket (z. B. `/data/baer-qwen`) |
| `AVATAR_FILE` | eingebautes Logo | eigenes Profilbild des Bots (PNG/JPEG, z. B. `/data/avatar.png`) |
| `STICKER_DIR` | – | Ordner mit Sticker-PNGs für `!sticker` (Dateiname = Name, ideal 512 × 512) |
| `BIRTHDAY_HOURS` | `12` | so lange läuft eine `!geburtstag`-Party |

**Eigener Bär (3D):** Eingebaut ist ein einfacher Bär aus Einzelbildern. Ein
richtig animiertes Paket entsteht mit Blender (z. B. auf einem Mac). Der Plüsch-Bär
ist ein durchgehender, weicher Körper (Metaballs) mit Partikel-Fell (gebüschelt, leicht
gelockt, nach unten gekämmt), an dieselben Gelenke gebunden wie die Clips. T-Shirt mit
Kragen und Ärmelsaum entlang der echten Schnittkanten, Pfotenballen (nur sichtbar, wenn
die Handfläche zum Publikum zeigt), Kopfhörer, Gegenlicht in Pink/Cyan. Gesicht mit
Glanzaugen, Brauen, weichen Bäckchen, Blinzeln, Zwinkern, konzentriertem und staunendem
Blick, Mitsingen; der Kopf folgt dem Körper leicht verzögert, Ohren schwingen nach, der
Beat-Akzent sitzt knackig auf der Eins. `--scale 1.5` rendert für 720p (~1,1 s pro Bild,
~65 min für alles):

```sh
B="blender -b --factory-startup -P tools/baer3d/baer3d.py --"
$B --out clips --scale 1.5 --desk                 # DJ-Pult
$B --out clips --scale 1.5 --stage                # Bühne
$B --out clips --scale 1.5 [--clips mix,wave]     # Clips (+ Ein-/Ausstiege)
$B --out sheet --sheet --clips dab,roof           # Posen-Vorschau (schnell)
$B --out frames --scale 1.5 --frame cheer:0.5     # ein Bild in voller Größe
$B --out clips --scale 1.5 --clips talk --edges-only  # nur Ein-/Ausstiege
python tools/baer3d/pack.py clips data/baer-paket # PNG -> WebP
```

`--model 1` baut den alten Kugel-Bären, `--fur-kids` und `--samples` tauschen
Qualität gegen Renderzeit.

**Saubere Übergänge:** Jeder Move hat einen Ein- und einen Ausstieg (je 1 Beat)
über eine gemeinsame Grundpose (Pfoten am Controller). Der Bot plant den Wechsel im
letzten Beat: Der alte Move läuft in die Grundpose aus, der neue steigt dort ein. Es
wird nicht mehr überblendet (keine halbdurchsichtigen Doppel-Arme). Spontane Wechsel:
beim Drop ein harter Schnitt unter dem Blitz, beim Losreden eine kurze Überblendung
(0,22 s). Zurück zum Tanzen geht es wieder über die Grundpose.

Jeder Clip ist ein 4-Beat-Loop. Es gibt 32 Moves in fünf Sorten, die der Bot
taktweise mischt (zuletzt gespielte werden gemieden). Welcher Move kommt, hängt vom
Song ab: Der Bot hört mit (Lautheit, Bass, Höhen, Tempo) und erkennt grob ruhige
Stellen, Build-ups, Breaks und Drops. Ruhige Songs bekommen Schunkeln und Herz,
Build-ups Schrauben am Pult, Drops Jubel, schnelle Songs mit vielen Hi-Hats Luftschlagzeug
und Roboter. Auch was er sagt, passt dazu: „Achtung, gleich kommt's!“ im Build-up,
„140 Beats pro Minute!“ bei schnellen Songs, nach dem Song „Vier Drops in einem Song!“
oder „Schön entspannt war das“.

| Sorte | Moves | wann |
|---|---|---|
| groove | mix, groove, pump, scratch, knobs, cue, sway, shimmy, lowpump | normal, wechselt oft |
| hype | headbang, cheer, jump, clap, spin, airdrum, fistpump, roof, disco, robot, crowdwave, dab | Drops, laute Stellen |
| special | wave, coffee, heart, thumbsup, stretch, lookout, bow | alle paar Takte, Kaffee in Stille |
| talk | talk, point, explain | Ansagen und Moderation |
| shout | shout | Zurufe mitten im Song |

Das Pult (Booth mit LED-Front, Controller mit Jogwheels, Mixer und Pads) wird aus
derselben Kamera gerendert, einmal gedimmt und einmal voll leuchtend. Der Bot blendet
LED-Front, Kanten, Pegel und Jogwheels live im Takt zwischen beiden Bildern.

Den Ordner nach `data/` legen (lesbar für UID 10000) und
`BAER_ASSETS=/data/baer-paket` setzen. Ein kaputtes Paket fällt auf den
eingebauten Bären zurück.

**Posen-Paket aus Standbildern:** Statt Blender-Loops gehen auch freigestellte Einzelbilder
(RGBA, alle gleich groß, z. B. aus einem Bildmodell). Sie landen auf Bühne und Pult eines
3D-Pakets, stehen hinter dem Pult und wippen per Code im Takt (`"bob": true` im Manifest).
Die erste Pose legt fest, wo Kopf und Füße sitzen; `--groesse` skaliert den Bären.

```sh
python tools/posen_paket.py --vorlage data/baer-paket --out data/baer-posen \
    groove:neutral=neutral.png groove:mix=mixen.png groove:sway=links.png,rechts.png \
    hype:cheer=jubeln.png talk:talk=sprechen-zu.png+sprechen-offen.png \
    shout:shout=rufen-zu.png+rufen-offen.png special:coffee=kaffee.png react:winken=winken.png
```

`a.png,b.png` ergibt einen Loop über 4 Beats, `+offen.png` das Bild mit offenem Mund
(Lippen-Sync beim Reden; außerhalb des Munds pixelgleich). Clips der Sorte `react` wählt der
Bär nie von selbst. Im Manifest lassen sich dann ergänzen (alles optional):

| Schlüssel | Wirkung |
|---|---|
| `events` | Ereignis → Clip: `hallo`/`tschuess` (jemand kommt in den Call / geht), `skip`, `lauter`, `leiser`, `favorit`, `plattenkiste` (`!play`), `ratlos` (Fehler), `blick`/`blick_lachen` (Blick), `pause` (hält, solange pausiert) |
| `moods` | Zusatz-Clips für `idle` (keine Musik), `calm` (ruhige Stellen), `night` (0–6 Uhr) |
| `seasons` | Kostüm je Saison: `{"weihnachten": {"clips": {"mix": "xmas_mix", …}, "stage": "weihnachten"}}`. Nach Datum: Weihnachten 1.–26.12., Silvester 31.12.–1.1., Halloween 25.–31.10., Sommer 21.6.–31.8.; `geburtstag` von Hand, `nacht` 0–6 Uhr (wenn sonst nichts ansteht) |
| `stage.variants` | weitere Bühnenbilder auf derselben Leinwand: `calm`, `build`, `drop` (wechseln mit dem Songteil, weich überblendet) und die Saison-Bühnen |
| `stage.front` | Publikum im Vordergrund, `down`/`up` (Hände hoch bei Drop und Hype) |
| `sprites` | Bilder für Partikel: `note`, `herz`, `stern`, `funke`, `konfetti_*` (Drop), `zzz_wolke` (Pause), `fragezeichen`, `ausrufezeichen` |
| `cover` | Ersatz-Titelbild für Titel ohne eigenes |

Die Bühne wird so skaliert, dass der Bär das Bild füllt: sichtbar ist nur der mittlere Teil
(bei 1680 × 960 etwa x 160–1520, y 140–900). Deko am Rand fällt weg.

## KI im Call: Blick (optional)

Der DJ kann im Element Call **hinschauen**. Das ist standardmäßig aus.

- **Blick** (`BLICK_ENABLED=true`): Alle paar Minuten (`BLICK_EVERY`, Standard 180 s ± 40 %)
  holt er gleichzeitig ein Standbild von allen eingeschalteten Kameras und geteilten
  Bildschirmen (höchstens 6, Bildschirme zuerst), stark verkleinert (Kamera 448 px,
  Bildschirm 640 px, JPEG 55) und schickt alle in **einer** Anfrage ans Bildmodell. Er sucht
  sich das Interessanteste aus und ruft einen Spruch über die Musik („Max, du zockst ja schon
  wieder!“). Hat sich kein Bild verändert, sagt er nichts.
- **Von Hand:** `!blick` schaut sofort hin (auch auf unveränderte Bilder), `!blick mich` nur auf
  deine Kamera/deinen Bildschirm, `!blick max` nur auf Max. Darf jeder, der im Call ist. Läuft
  gerade Musik, ruft er den Spruch darüber (wartet bis zu 20 s auf eine Lücke), sonst schreibt
  er ihn in den Chat.
  Das Modell soll nie Aussehen/Körper bewerten und nichts Privates vom Bildschirm vorlesen.

**Datenschutz:** Bilder werden nicht gespeichert; sie gehen nur an den konfigurierten
Endpunkt (`KI_URL` – nimm einen eigenen Server). Mikrofone abonniert der Bot nie. Beim ersten
Beitritt sagt er im Raum, dass er schaut. Jede Person schaltet es für sich ab: `!blick aus`
(bleibt gespeichert).

**Verschlüsselte Calls:** Element Call schickt jedem Teilnehmer seinen Medien-Schlüssel als
Olm-To-Device (`io.element.call.encryption_keys`). matrix-nio verwirft diesen Typ; der Bot fängt
ihn ab und gibt die Schlüssel an LiveKit weiter, sonst bekäme er nur verschlüsselte Pakete.

| Variable | Standard | Bedeutung |
|---|---|---|
| `KI_URL` · `KI_KEY` · `KI_MODEL` | – | OpenAI-kompatibler Endpunkt mit Bild-Eingang (Antwort gestreamt) |
| `KI_TIMEOUT` | `120` | Sekunden bis eine Antwort verworfen wird |
| `BLICK_ENABLED` · `BLICK_EVERY` | `false` · `180` | Blick an, Abstand in Sekunden |

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
