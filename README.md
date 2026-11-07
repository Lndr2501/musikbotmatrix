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

| Aktion | Cache + Bibliothek |
|---|---|
| `docker compose restart`, Reboot, `up -d`, Image-Neubau | **bleiben erhalten** |
| `docker compose stop` / `down` | **bleiben erhalten** (Volume bleibt) |
| `docker compose down -v` | **alles weg** (Cache, Bibliothek, DB, Gerät) |
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
| `!seek <mm:ss>` · `!volume <0-100>` · `!video on|off` | spulen / lauter / Bild an aus |
| `!shuffle` · `!loop on|off` | mischen / aktueller Titel in Dauerschleife |
| `!joincall` · `!leavecall` | Bot ruft alle im Raum an / haengt auf |
| `!cache` · `!library` · `!unsave <Nr.>` | Speicher-Status ansehen / verwalten |
| `!help` | Kommandoliste |

Deutsche Aliase funktionieren auch (`!abspielen`, `!pausieren`, `!fortsetzen`,
`!hilfe`, …). Praefix: `!` (konfigurierbar via `BOT_PREFIXES`).

## Speicher-Regeln (genau so, wie gewuenscht)

- Ein Titel wird ab **3 Anforderungen** (Default) dauerhaft in `library/`
  (im Volume `/data`) gespeichert und nie automatisch geloescht.
- Sonst liegt er in `cache/` mit maximal **10 Titeln**.
- Ein frischer Cache-Titel fliegt raus, wenn er **2 weitere Anforderungen**
  braucht, bevor **9 andere Titel** angefordert wurden.
- Alles per `.env` einstellbar: `SAVE_AFTER_REQUESTS`, `CACHE_MAX_ENTRIES`,
  `CACHE_WINDOW_SELF`, `CACHE_WINDOW_OTHERS`.

Beispiel: Titel A kommt rein. Laufen danach 9 andere Titel, ohne dass A nochmal
gewaehlt wird, loescht der Bot A inkl. Datei. Kommt A zweimal wieder, ist er
“gesetzt” und bleibt bis zur 3. Gesamtanforderung in der Bibliothek.

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
