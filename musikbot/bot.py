"""Musikbot fuer klassische Matrix-Calls.

Verdrahtet: Matrix (nio) -> Befehle -> yt-dlp -> Speicher-Policy -> Player
-> WebRTC-Peers. Der Bot ist ein normaler Matrix-Nutzer und betritt Calls
ueber das Telefon-Icon (Peer-zu-Peer/mesh, klassisches m.call-Signaling).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time

import aiohttp

from . import cards
from .calls import CallManager
from .commands import HELP, is_read_only, parse
from .elementcall import ElementCall
from .config import Config
from .hub import Hub
from .matrix import Matrix
from .player import Player
from .store import TrackStore
from .util import fmt_time, now_ms, parse_time
from .webauth import WebAuth
from .ytdl import MediaError, MediaResolver

log = logging.getLogger("musikbot")

AUDIO_VIDEO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma",
                   ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".3gp"}


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def probe_duration(path: str) -> float:
    """Laenge in Sekunden; wirft, wenn die Datei kein Audio/Video enthaelt."""
    import av
    with av.open(path) as c:
        if not any(s.type in ("audio", "video") for s in c.streams):
            raise ValueError("kein Audio/Video")
        return float(c.duration / av.time_base) if c.duration else 0.0


RADIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma"}
PLAYLIST_NAME = re.compile(r"^[\w.-]{1,32}$")
MAX_SHOWN = 30


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = TrackStore(cfg.db_path, cfg.tmp_dir, cfg.cache_dir, cfg.library_dir,
                               hot_dir=cfg.hot_dir, hot_after=cfg.hot_after, hot_days=cfg.hot_days,
                               keep_dirs=(cfg.radio_dir,))
        self._fetch_room = ""
        self.hub = Hub()
        self.player = Player(cfg, self.hub, self.store,
                             on_event=self._player_event, fetch=self._fetch)
        self.calls = CallManager(cfg, self.player)
        self.matrix = Matrix(cfg)
        self.ecall = ElementCall(cfg, self.matrix, self.player)
        self.resolver = MediaResolver(cfg)
        # Buchhaltung, welcher Raum welche Liste zuletzt gesehen hat:
        self._listing: dict[str, list[str]] = {}
        self._history: list[str] = []
        self._loop_on = False
        self._fetching = 0
        self._busy: set[str] = set()
        # Karten (Diddy): Suche/Download-Karte eines Raums und die Karte des laufenden Titels
        self._card_pending: dict[str, str] = {}
        self._card_now: dict[str, tuple[str, object]] = {}
        # Titelbilder: Quelle (http) und die auf den Medien-Server geladene mxc://-Adresse je Titel
        self._thumb_url: dict[str, str] = {}
        self._thumb_mxc: dict[str, str] = {}
        self._cover_frames: dict[str, object] = {}
        self._cmds_done: set[str] = set()
        self._cmds_failed: dict[str, float] = {}
        self._thumb_tasks: dict[str, asyncio.Task] = {}
        # 24/7-Modus: None oder {"kind": "song"|"playlist", "keys": [...], "name": str}
        self._always: dict | None = None
        self._radio_files: dict[str, str] = {}      # Radio-Key -> Dateipfad
        self.ecall.keep_alive = lambda: self._always is not None
        # Web-Dashboard: Login-Daten liegen in derselben Datenbank
        self.webauth = WebAuth(self.store.db, self.store._lock)
        self.dashboard = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._names: dict[str, str] = {}            # Anzeigenamen fuers Dashboard
        self._load_fade_settings()

        m = self.matrix
        m.on_text = self._on_text
        m.on_media = self._on_media
        m.on_call = self.calls.handle_event
        m.on_member_join = self.calls.on_member_joined
        m.on_room_left = self.calls.on_room_left
        m.on_joined = self._publish_commands
        m.on_rtc_member = self.ecall.on_member_event
        self.calls.send_room_event = m.send_room_event
        self.calls.send_to_device = m.send_to_device
        self.calls.devices_of_room = m.devices_of_room

    # ------------------------------------------------------------------ Start
    async def startup(self) -> list[str]:
        await self.matrix.login()
        rooms = await self.matrix.prepare_rooms()
        for rid in rooms:
            await self.matrix.ensure_room_state(rid)
            await self._publish_commands(rid)
        turn = await self.matrix.fetch_turn()
        if turn:
            self.calls.set_turn(turn)
        return rooms

    async def run(self) -> None:
        rooms = await self.startup()
        log.info("Bot bereit. Raeume: %s", ", ".join(rooms) or "(keine konfiguriert)")
        idle = asyncio.create_task(self._idle_watch(), name="idle-leave")
        try:
            await self.matrix.run()
        finally:
            idle.cancel()

    async def _idle_watch(self) -> None:
        """Verlaesst den Call, wenn IDLE_LEAVE_SECONDS lang nichts spielt."""
        limit = self.cfg.idle_leave_seconds
        if limit <= 0:
            return
        idle_since: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(5)
            try:
                in_call = self.ecall.active or bool(self.calls.peers)
                busy = self.player.state == "playing" or self._fetching > 0
                if not in_call or busy or self._always:
                    idle_since = None
                    continue
                if idle_since is None:
                    idle_since = loop.time()
                elif loop.time() - idle_since >= limit:
                    idle_since = None
                    room = self.ecall.room_id or self.calls.active_room
                    log.info("Idle seit %ss, verlasse den Call", limit)
                    await self.ecall.leave()
                    await self.calls.hangup_all("idle")
                    if room:
                        await self.matrix.send_notice(
                            room, f"👋 Nichts gespielt seit {limit} s – Call verlassen.")
            except Exception:
                log.exception("Idle-Check fehlgeschlagen")

    async def start_web(self) -> None:
        """Dashboard starten (nach dem Login, nicht im Selbsttest)."""
        if not self.cfg.web_enabled:
            return
        from .web import Dashboard
        self.dashboard = Dashboard(self)
        try:
            await self.dashboard.start()
        except OSError:
            log.exception("Dashboard-Port %d belegt – Dashboard aus", self.cfg.web_port)
            self.dashboard = None

    async def shutdown(self) -> None:
        if self.dashboard is not None:
            await self.dashboard.stop()
        try:
            await self.ecall.leave()
        except Exception:
            log.exception("Element Call verlassen beim Shutdown")
        await self.calls.hangup_all("shutdown")
        await self.player.shutdown()
        self.matrix.stop()
        self.store.close()

    # ------------------------------------------------------------------ Karten
    def _track_meta(self, track) -> dict:
        return {"artist": getattr(track, "uploader", "") or "", "duration": track.duration or 0.0,
                "url": getattr(track, "url", "") or "",
                "thumbnail": self._thumb_mxc.get(track.key, "")}

    # --- Titelbild (Albumcover bzw. Video-Vorschaubild) -------------------------------------
    def _thumb_source(self, key: str) -> str:
        """Adresse des Titelbilds: von yt-dlp gemeldet, sonst bei YouTube aus der Video-ID."""
        url = self._thumb_url.get(key, "")
        if url:
            return url
        ext, _, vid = key.partition(":")
        if ext == "youtube" and re.fullmatch(r"[\w-]{6,20}", vid):
            return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"      # 16:9, ohne schwarze Balken
        return ""

    async def _fetch_thumb(self, key: str) -> str:
        """Titelbild laden und hochladen; mxc://-Adresse oder "" (nie ein Fehler)."""
        if key in self._thumb_mxc:
            return self._thumb_mxc[key]
        url = self._thumb_source(key)
        if not url:
            return ""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(url) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if resp.status != 200 or not ctype.startswith("image/"):
                        return ""
                    # read(n) liefert nur, was gerade im Puffer liegt: in Stuecken bis zum Ende lesen
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        buf += chunk
                        if len(buf) > 3_000_000:
                            return ""
                    data = bytes(buf)
            if not data:
                return ""
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(ctype, "img")
            mxc = await self.matrix.upload_image(data, ctype, f"cover.{ext}")
            self._cover_frame_for(key, data)
            if mxc:
                self._thumb_mxc[key] = mxc
            return mxc or ""
        except Exception:
            log.debug("Titelbild nicht verfuegbar (%s)", key, exc_info=True)
            return ""

    def _cover_frame_for(self, key: str, data: bytes) -> None:
        """Titelbild als Kamerabild vorbereiten (nur fuer den laufenden Titel noetig)."""
        from .media import cover_frame
        try:
            h = int(self.cfg.video_height) // 2 * 2
            frame = cover_frame(data, (h * 16 // 9) // 2 * 2, h)
        except Exception:                      # das Kamerabild darf die Karte nie verhindern
            log.debug("Titelbild als Kamerabild fehlgeschlagen", exc_info=True)
            return
        if frame is not None:
            self._cover_frames[key] = frame
            while len(self._cover_frames) > 4:
                self._cover_frames.pop(next(iter(self._cover_frames)))

    def _prefetch_thumb(self, key: str) -> None:
        """Titelbild schon waehrend Suche/Download laden, damit es zur Karte fertig ist."""
        if key in self._thumb_mxc or key in self._thumb_tasks:
            return
        self._thumb_tasks[key] = asyncio.create_task(self._fetch_thumb(key))

    async def _thumb_ready(self, key: str, wait: float = 4.0) -> str:
        """Titelbild fuer die Karte: laeuft das Laden noch, hoechstens `wait` Sekunden darauf warten."""
        if key in self._thumb_mxc:
            return self._thumb_mxc[key]
        self._prefetch_thumb(key)
        try:
            return await asyncio.wait_for(asyncio.shield(self._thumb_tasks[key]), wait)
        except Exception:
            return ""

    async def _note(self, room: str, text: str, tone: str = "info", icon: str = "") -> None:
        """Kurze Meldung als Hinweis-Chip (Diddy); andere Clients sehen den Text."""
        await self.matrix.send_card(
            room, cards.card_content(cards.note_body(text, icon), cards.note_card(text, tone, icon)))

    async def _put_track_card(self, room: str, card: dict, eid: str | None) -> str | None:
        """Titel-Karte senden oder (mit event_id) bearbeiten. Gibt die event_id der Karte zurueck."""
        content = cards.card_content(cards.track_body(card), card)
        if eid and await self.matrix.edit_card(room, eid, content):
            return eid
        return await self.matrix.send_card(room, content)

    async def _fail_pending(self, room: str, title: str, text: str) -> None:
        """Die Such-/Download-Karte wird zur Fehlerkarte (sonst bliebe sie auf "laeuft")."""
        eid = self._card_pending.pop(room, None)
        card = cards.track_card("error", title, note=text)
        await self._put_track_card(room, card, eid)

    async def _finish_now_card(self, room: str, state: str = "stopped") -> None:
        """Karte des bisherigen Titels auf "gestoppt" setzen (kein Laufbalken, keine Knoepfe mehr)."""
        entry = self._card_now.pop(room, None)
        if not entry:
            return
        eid, track = entry
        card = cards.track_card(state, track.title or track.key, **self._track_meta(track))
        await self._put_track_card(room, card, eid)

    async def _now_card_state(self, room: str, state: str) -> bool:
        """Karte des laufenden Titels auf "playing"/"paused" setzen. False: es gibt keine Karte."""
        entry = self._card_now.get(room)
        if not entry:
            return False
        eid, track = entry
        pos = self.player.position
        if state == "playing":
            card = cards.track_card("playing", track.title or track.key, position=pos,
                                    started_at_ms=now_ms(), actions=cards.controls(False, self._loop_on),
                                    seek=cards.SEEK_COMMAND, volume=self.player.volume,
                                    **self._track_meta(track))
        else:
            card = cards.track_card("paused", track.title or track.key, position=pos,
                                    actions=cards.controls(True, self._loop_on), seek=cards.SEEK_COMMAND,
                                    volume=self.player.volume, **self._track_meta(track))
        content = cards.card_content(cards.track_body(card), card)
        return await self.matrix.edit_card(room, eid, content)

    # ------------------------------------------------------------------ Events
    def _notice_room(self) -> str | None:
        """Raum, in den Statusmeldungen gehen (Element Call oder klassischer Call)."""
        return self.ecall.room_id or self.calls.active_room or self._fetch_room or None

    async def _leave_call(self, room: str | None, text: str) -> None:
        if not (self.ecall.active or self.calls.peers):
            if room:
                await self.matrix.send_notice(room, "Warteschlange ist leer.")
            return
        await self.ecall.leave()
        await self.calls.hangup_all("done")
        if room:
            await self.matrix.send_notice(room, text)

    def _radio_scan(self) -> list[str]:
        """Radio-Ordner einlesen (jedes Mal neu: neue Dateien sind sofort dabei)."""
        root = self.cfg.radio_dir
        keys: list[str] = []
        files: dict[str, str] = {}
        for base, _dirs, names in os.walk(root):
            for name in sorted(names):
                if name.startswith(".") or os.path.splitext(name)[1].lower() not in RADIO_EXT:
                    continue
                path = os.path.join(base, name)
                key = "radio:" + hashlib.sha1(os.path.relpath(path, root).encode()).hexdigest()[:16]
                files[key] = path
                keys.append(key)
                self.store.note_request(key, title=os.path.splitext(name)[0][:120],
                                        url="file://" + path, uploader="Radio")
        self._radio_files = files
        return keys

    def _radio_shuffled(self, last: str | None = None) -> list[str]:
        keys = self._radio_scan()
        random.shuffle(keys)
        if len(keys) > 1 and keys[0] == last:      # nicht zweimal hintereinander denselben
            keys[0], keys[-1] = keys[-1], keys[0]
        return keys

    def _refill_always(self) -> bool:
        """24/7: Warteschlange wieder fuellen. False = nichts (mehr) zu spielen."""
        cfg = self._always
        if not cfg:
            return False
        if cfg["kind"] == "playlist":
            keys = self.store.playlist_keys(cfg["name"]) or []
        elif cfg["kind"] == "radio":
            keys = self._radio_shuffled(getattr(self.player.current, "key", None))
        else:
            keys = cfg["keys"]
        if not keys:
            self._always = None
            return False
        for key in keys:
            self.player.enqueue(key, "24/7")
        return True

    async def _player_event(self, name: str, data: dict) -> None:
        if name == "track-end":
            if self._loop_on and data.get("track") is not None:
                self.player.enqueue(data["track"].key, "")
            elif self._always and not self.player.queue:
                self._refill_always()
            return
        room = self._notice_room()
        if not room:
            return
        if name == "advanced":
            await self._announce(data.get("track"))
        elif name == "missing":
            key = data.get("key", "")
            await self._note(room, f"Titel ({key}) ist nicht mehr verfuegbar.", "warning", "⚠️")
        elif name == "queue-empty":
            if not data.get("auto"):
                return
            await self._finish_now_card(room)
            if self._always:
                await self._note(
                    room, "24/7: Es ließ sich nichts mehr abspielen. "
                          "Mit !24/7 off beenden oder neu starten.", "warning")
            elif self.cfg.leave_when_done and not self._busy and not self._fetching:
                log.info("Warteschlange leer und Titel zu Ende, verlasse den Call")
                await self._leave_call(
                    room, "👋 Warteschlange leer und Song zu Ende – Call verlassen.")
            else:
                await self._note(room, "Warteschlange ist leer – ich pausiere.")

    async def _announce(self, track) -> None:
        room = self._notice_room()
        if not room or track is None:
            return
        self._log_play(track)
        pending = self._card_pending.pop(room, None)
        if pending is None:
            await self._finish_now_card(room)       # naechster Titel aus der Warteschlange
        await self._thumb_ready(track.key)          # Titelbild gehoert in die Karte
        self.player.set_cover(track.key, self._cover_frames.get(track.key))   # und in die Kamera
        card = cards.track_card(
            "playing", track.title or track.key, position=0.0, started_at_ms=now_ms(),
            actions=cards.controls(False, self._loop_on), seek=cards.SEEK_COMMAND, volume=self.player.volume,
            **self._track_meta(track))
        eid = await self._put_track_card(room, card, pending)
        if eid:
            self._card_now[room] = (eid, track)

    # ------------------------------------------------------------ Favoriten / Statistik
    def _log_play(self, track) -> None:
        """Wiedergabe fuer die Statistik merken (nur echte Nutzer, nicht 24/7 oder Wiederholung)."""
        who = getattr(self.player, "requester", "") or ""
        if who.startswith("@"):
            try:
                self.store.play_log(track.key, who, track.duration)
            except Exception:
                log.warning("Statistik schreiben fehlgeschlagen", exc_info=True)

    @staticmethod
    def _who(user: str) -> str:
        return user[1:].split(":", 1)[0] if user.startswith("@") else user

    def _fav_items(self, keys: list[str]) -> list[dict]:
        out = []
        for key in keys:
            t = self.store.get(key)
            item = {"title": (t.title if t else "") or key,
                    "subtitle": (t.uploader if t and t.uploader else None),
                    "duration": (t.duration if t and t.duration else None)}
            out.append({k: v for k, v in item.items() if v is not None})
        return out

    async def _fav(self, room_id: str, sender: str, arg: str) -> None:
        m, st = self.matrix, self.store
        sub, _, rest = arg.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub == "":
            cur = self.player.current
            if cur is None:
                await self._note(room_id, "Gerade läuft nichts. Mit !fav list siehst du deine Favoriten.",
                                 "warning", "⭐")
                return
            added = st.fav_add(sender, cur.key)
            await self._note(room_id, (f"„{cur.title or cur.key}“ ist jetzt in deinen Favoriten."
                                       if added else f"„{cur.title or cur.key}“ ist schon in deinen Favoriten."),
                             "success" if added else "info", "⭐")
        elif sub in ("list", "liste", "show"):
            keys = st.fav_keys(sender)
            items = self._fav_items(keys)
            self._listing[room_id] = keys
            title = f"Favoriten von {self._who(sender)}"
            acts = (cards.action("▶ Alle abspielen", "!fav play"),
                    cards.action("🔀 Gemischt", "!fav play shuffle")) if items else ()
            await m.send_card(room_id, cards.card_content(
                cards.list_body(title, items, "noch keine"),
                cards.list_card(title, items[:MAX_SHOWN], len(items), acts,
                                empty="Noch keine. Mit !fav merkst du dir den laufenden Titel.")))
        elif sub in ("remove", "rm", "del", "entfernen"):
            if not rest.isdigit() or st.fav_remove(sender, int(rest)) is None:
                await self._note(room_id, "Benutzung: !fav remove <nr> (Nummer aus !fav list)", "warning")
                return
            await self._note(room_id, "Aus deinen Favoriten entfernt.", "success", "⭐")
        elif sub in ("play", "spielen"):
            keys = st.fav_keys(sender)
            if not keys:
                await self._note(room_id, "Du hast noch keine Favoriten.", "warning", "⭐")
                return
            if rest.lower() in ("shuffle", "mischen"):
                random.shuffle(keys)
            await self._play_keys(room_id, sender, keys, replace=True)
        else:
            await self._note(room_id, "Benutzung: !fav · !fav list · !fav remove <nr> · !fav play [shuffle]",
                             "warning")

    async def _stats(self, room_id: str) -> None:
        st = self.store
        tracks = [{"label": self._title(k), "value": n} for k, n in st.top_tracks(8)]
        people = [{"label": self._who(u), "value": round(sec / 60, 1), "sub": f"{n} Titel"}
                  for u, n, sec in st.top_listeners(8)]
        if not tracks:
            await self._note(room_id, "Noch keine Statistik – spiel erst ein paar Titel.", "info", "📊")
            return
        charts = [{"title": "Meistgespielte Titel", "unit": "×", "items": tracks},
                  {"title": "Wer hört am meisten Musik", "unit": "min", "items": people}]
        await self.matrix.send_card(room_id, cards.card_content(
            cards.stats_body("Musik-Statistik", charts), cards.stats_card("Musik-Statistik", charts)))

    # ------------------------------------------------------------------ Text
    async def _publish_commands(self, room_id: str) -> None:
        """Befehle im Raum anmelden (State-Event), damit Clients beim Tippen vorschlagen koennen.
        Braucht das Recht, Raumzustand zu setzen; fehlt es, bleibt es bei einer Warnung."""
        if room_id in self._cmds_done:
            return
        last = self._cmds_failed.get(room_id, 0.0)
        if time.monotonic() - last < 60:           # fehlgeschlagen: hoechstens einmal pro Minute neu versuchen
            return
        self._cmds_failed.pop(room_id, None)
        uid = self.cfg.matrix_user_id or self.cfg.user
        try:
            current = await self.matrix.get_state_events(room_id, cards.COMMANDS_EVENT)
            want = cards.commands_content(self.cfg.prefixes[0] if self.cfg.prefixes else "!")
            have = {k: v for k, v in (current.get(uid) or {}).items() if k != "_sender"}
            if have != want:
                await self.matrix.put_state(room_id, cards.COMMANDS_EVENT, uid, want)
            self._cmds_done.add(room_id)
        except Exception as exc:
            self._cmds_failed[room_id] = time.monotonic()
            log.warning("Befehle in %s nicht angemeldet (Rechte?): %s", room_id, exc)

    async def _on_text(self, room_id: str, sender: str, body: str) -> None:
        await self._publish_commands(room_id)
        cmd = parse(body, self.cfg.prefixes)
        if cmd is None:
            return
        if not self._allowed(sender):
            await self._note(room_id, "Das darfst du hier nicht.", "warning", "🚫")
            return
        if (self.cfg.call_only_control and not is_read_only(cmd.name, cmd.arg)
                and not await self._in_call(room_id, sender)):
            await self._note(
                room_id, "Steuern können nur Leute, die selbst im Call sind. "
                         "Tritt dem Call bei und versuch es nochmal.", "warning", "🔒")
            return
        try:
            await self._dispatch(room_id, sender, cmd.name, cmd.arg)
        except MediaError as exc:
            await self._note(room_id, str(exc), "error", "❌")
        except Exception:
            log.exception("Command %s fehlgeschlagen", cmd.name)
            await self._note(room_id, "Interner Fehler – siehe Log.", "error", "❌")

    async def _on_media(self, room_id: str, sender: str, name: str, mime: str,
                        data: bytes) -> None:
        """Angehaengte Audio-/Videodatei direkt abspielen."""
        ext = os.path.splitext(name)[1].lower()
        if not (mime.startswith(("audio/", "video/")) or ext in AUDIO_VIDEO_EXT):
            return                                   # normaler Dateianhang, nicht fuer uns
        if not self._allowed(sender):
            await self._note(room_id, "Das darfst du hier nicht.", "warning", "🚫")
            return
        if (self.cfg.call_only_control and not await self._in_call(room_id, sender)):
            await self._note(
                room_id, "Steuern können nur Leute, die selbst im Call sind. "
                         "Tritt dem Call bei und schick die Datei nochmal.", "warning", "🔒")
            return
        if not data:
            await self._note(room_id, f"Datei zu groß (maximal {self.cfg.max_upload_mb} MB).",
                             "error", "❌")
            return
        if room_id in self._busy:
            await self._note(room_id, "Ich lade gerade schon etwas …", "info", "⏳")
            return
        self._busy.add(room_id)
        try:
            await self._play_upload(room_id, sender, name, data, ext)
        except MediaError as exc:
            await self._note(room_id, str(exc), "error", "❌")
        except Exception:
            log.exception("Anhang %s fehlgeschlagen", name)
            await self._note(room_id, "Interner Fehler – siehe Log.", "error", "❌")
        finally:
            self._busy.discard(room_id)

    async def _play_upload(self, room_id: str, sender: str, name: str, data: bytes,
                           ext: str) -> None:
        key = "upload:" + hashlib.sha1(data).hexdigest()[:16]
        os.makedirs(self.cfg.tmp_dir, exist_ok=True)
        path = os.path.join(self.cfg.tmp_dir, key.replace(":", "_") + (ext or ".bin"))
        await asyncio.to_thread(_write_file, path, data)
        try:
            duration = await asyncio.to_thread(probe_duration, path)
        except Exception as exc:
            os.remove(path)
            raise MediaError("Diese Datei kann ich nicht abspielen.") from exc
        limit = self.cfg.max_song_seconds
        if limit and duration > limit:
            os.remove(path)
            raise MediaError(f"Zu lang ({fmt_time(duration)}). Maximal {fmt_time(limit)}.")
        title = os.path.splitext(name)[0][:120] or "Datei"
        track = self.store.note_request(key, title=title, duration=duration,
                                        uploader=self._who(sender))
        self.store.set_file(key, path)
        self._fetch_room = room_id
        self._history.append(key)
        del self._history[:-20]
        await self._start_key(room_id, key, sender, announce=True)

    async def _in_call(self, room_id: str, sender: str) -> bool:
        """Ist der Absender gerade selbst im Call dieses Raums?"""
        who = sender.lower()
        try:
            await self.ecall.load_members(room_id)  # frischer Stand vom Homeserver
        except Exception:
            log.warning("Call-Mitglieder laden fehlgeschlagen, nutze Zwischenspeicher",
                        exc_info=True)
        if who in self.ecall.users_in_call(room_id):
            return True
        return any(p.live and p.answered and p.room_id == room_id
                   and p.user_id.lower() == who for p in self.calls.peers)

    def _allowed(self, sender: str) -> bool:
        if not self.cfg.allowed_users:
            return True
        return sender.lower() in {u.lower() for u in self.cfg.allowed_users}

    async def _dispatch(self, room_id: str, sender: str, name: str,
                        arg: str) -> None:
        m = self.matrix
        p = self.player
        if name == "play":
            if not arg:
                await m.send_notice(room_id, "Benutzung: !play <Link oder Suchbegriff>")
                return
            if room_id in self._busy:
                await m.send_notice(room_id, "Ich lade gerade schon etwas herunter …")
                return
            self._busy.add(room_id)
            try:
                await self._play(room_id, sender, arg)
            finally:
                self._busy.discard(room_id)
        elif name == "pause":
            ok = p.pause()
            if not (ok and await self._now_card_state(room_id, "paused")):
                await self._note(room_id, "pausiert." if ok else "Läuft gerade nicht.",
                                 "info" if ok else "warning", "⏸" if ok else "")
        elif name == "resume":
            ok = p.resume()
            if not (ok and await self._now_card_state(room_id, "playing")):
                await self._note(room_id, "weiter." if ok else "Nichts pausiert.",
                                 "info" if ok else "warning", "▶" if ok else "")
        elif name == "stop":
            was_always, self._always = self._always is not None, None
            await p.stop()
            await self._finish_now_card(room_id)
            await self._note(room_id, "gestoppt." + (" 24/7 ist aus." if was_always else ""),
                             "info", "⏹")
        elif name == "skip":
            if self._loop_on:
                self._loop_on = False
            if self._always and not p.queue:
                self._refill_always()
            ok = await p.skip()
            if not ok:
                # kein naechster Titel: die Wiedergabe ist beendet, die Karte nicht auf "Spielt" stehen lassen
                await self._finish_now_card(room_id)
                await self._note(room_id, "Keine weiteren Titel in der Warteschlange.", "warning", "⏭")
        elif name == "previous":
            if len(self._history) >= 2:
                prev = self._history[-2]
                self._history = self._history[:-1]
                await self._start_key(room_id, prev, sender)
            else:
                await m.send_notice(room_id, "Kein vorheriger Titel.")
        elif name == "queue":
            await self._show_queue(room_id)
        elif name == "clear":
            n = p.clear_queue()
            await self._note(room_id, f"Warteschlange geleert ({n} Titel).", "success", "🗑")
        elif name == "status":
            await self._show_status(room_id)
        elif name in ("seek", "forward", "rewind"):
            a = arg.strip()
            if name == "seek" and a[:1] not in ("+", "-"):
                seconds = parse_time(a)
                target = seconds
            else:
                if name == "seek":
                    sign, a = (1 if a[0] == "+" else -1), a[1:]
                else:
                    sign = 1 if name == "forward" else -1
                delta = parse_time(a) if a else 10.0
                target = None if delta is None else max(0.0, p.position + sign * delta)
            if target is None:
                await m.send_notice(room_id, "Benutzung: !seek <mm:ss | +30 | -30> · !vor [s] · !zurueck [s]")
                return
            cur = p.status()
            if cur["duration"] and target >= cur["duration"] - 1:
                target = max(0.0, cur["duration"] - 2)
            ok = await p.seek(target)
            state = "paused" if p.state == "paused" else "playing"
            if not (ok and await self._now_card_state(room_id, state)):
                await self._note(room_id, f"{fmt_time(p.position)}" if ok else "Kann gerade nicht gespult werden.",
                                 "info" if ok else "warning", "⏩" if ok else "")
        elif name == "volume":
            a = arg.strip()
            try:
                # "+10" / "-10": relativ zur aktuellen Lautstaerke (Knoepfe der Karte), sonst absolut
                value = p.volume + int(a) if a[:1] in "+-" and a[1:].isdigit() else int(a)
            except ValueError:
                await self._note(room_id, f"Lautstärke ist {p.volume}. Benutzung: !volume 0-300 "
                                          "(60 = Original, darüber Boost) · !volume +10 / -10", "info", "🔊")
                return
            v = p.set_volume(value)
            state = "paused" if p.state == "paused" else "playing"
            if not await self._now_card_state(room_id, state):
                await self._note(room_id, f"Lautstärke: {v}", "info", "🔊")
        elif name == "video":
            mode = arg.strip().lower()
            on = p.set_video(True if mode == "on" else False if mode == "off" else None)
            await m.send_notice(room_id, f"Video: {'an' if on else 'aus'}")
        elif name == "shuffle":
            random.shuffle(p.queue)
            await self._note(room_id, "Warteschlange gemischt." if p.queue else "Warteschlange ist leer.",
                             "success" if p.queue else "warning", "🔀" if p.queue else "")
        elif name == "loop":
            mode = arg.strip().lower()
            self._loop_on = True if mode == "on" else False if mode == "off" else not self._loop_on
            state = "paused" if self.player.state == "paused" else "playing"
            if not await self._now_card_state(room_id, state):      # Karte zeigt es selbst im Knopf
                await self._note(room_id, f"Schleife: {'an' if self._loop_on else 'aus'}", "info", "🔁")
        elif name == "joincall":
            if await self._join_element_call(room_id, force=True):
                await m.send_notice(room_id, "📞 Bin im Element Call.")
        elif name == "leavecall":
            await self.ecall.leave()
            await self.calls.hangup_all("user")
            await m.send_notice(room_id, "Call verlassen.")
        elif name == "playlist":
            await self._playlist(room_id, sender, arg)
        elif name == "fav":
            await self._fav(room_id, sender, arg)
        elif name == "stats":
            await self._stats(room_id)
        elif name == "always":
            await self._always_cmd(room_id, sender, arg)
        elif name == "dashboard":
            await self._send_dashboard_login(room_id, sender)
        elif name == "help":
            await m.send_card(room_id, cards.card_content(
                HELP, cards.help_card(cards.HELP_GROUPS, cards.HELP_NOTE)))
        else:  # pragma: no cover
            await m.send_notice(room_id, "Unbekanntes Kommando. !help")

    # ------------------------------------------------------------------ Play
    async def _play(self, room_id: str, sender: str, query: str) -> None:
        m = self.matrix
        title = query[:80]
        card = cards.track_card("searching", title)
        eid = await m.send_card(room_id, cards.card_content(cards.track_body(card), card))
        if eid:
            self._card_pending[room_id] = eid
        try:
            resolved = await self.resolver.resolve_async(query)
        except MediaError as exc:
            await self._fail_pending(room_id, title, str(exc))
            return
        except Exception:
            await self._fail_pending(room_id, title, "Interner Fehler – siehe Log.")
            raise
        try:
            await self._book_and_play(room_id, sender, resolved, query)
        except Exception:
            if room_id in self._card_pending:
                await self._fail_pending(room_id, resolved.title or title, "Interner Fehler – siehe Log.")
            raise

    async def _book_and_play(self, room_id: str, sender: str, resolved,
                             query: str) -> None:
        m = self.matrix
        st = self.store
        limit = self.cfg.max_song_seconds
        if limit and resolved.duration > limit:
            await self._fail_pending(
                room_id, resolved.title,
                f"Zu lang ({fmt_time(resolved.duration)}). Maximal {fmt_time(limit)}.")
            return
        track = st.note_request(
            resolved.key, title=resolved.title, url=resolved.url,
            duration=resolved.duration, uploader=resolved.uploader)
        if getattr(resolved, "thumbnail", ""):
            self._thumb_url[resolved.key] = resolved.thumbnail
        self._prefetch_thumb(resolved.key)
        self._fetch_room = room_id

        if self._loop_on and self.player.current and self.player.current.key == track.key:
            self.player.enqueue(track.key, sender)
            self._card_pending.pop(room_id, None)
            await self._note(room_id, "Wiederholung eingeplant.", "info", "🔁")
            return

        # Laeuft schon etwas (oder wird gerade geladen): hinten anstellen statt abloesen.
        if self.player.state in ("playing", "paused") or self.player._lock.locked():
            self.player.enqueue(track.key, sender)
            pos = len(self.player.queue)
            eid = self._card_pending.pop(room_id, None)
            card = cards.track_card("stopped", track.title or track.key,
                                    note=f"In der Warteschlange (#{pos})", **self._track_meta(track))
            await self._put_track_card(room_id, card, eid)
            return

        self._history.append(track.key)
        del self._history[:-20]
        await self._start_key(room_id, track.key, sender, announce=True)

    async def _fetch(self, track, *, quiet: bool = False) -> None:
        """Titel fuer die Wiedergabe holen: gemerkt (oft gespielt) oder frisch herunterladen.

        quiet=True: Vorladen fuer den Crossfade, waehrend noch ein Titel laeuft –
        dann keine "laedt herunter"-Karte im Chat."""
        if track.key.startswith("radio:"):           # Radio-Ordner: Datei liegt dort
            path = self._radio_files.get(track.key, "")
            if not path or not os.path.exists(path):
                raise MediaError("Radio-Datei ist nicht mehr vorhanden.")
            if not track.duration:
                try:
                    self.store.note_request(track.key, duration=await asyncio.to_thread(probe_duration, path))
                except Exception as exc:
                    raise MediaError("Diese Radio-Datei kann ich nicht abspielen.") from exc
            self.store.set_file(track.key, path)
            return
        if track.key.startswith("upload:"):          # Chat-Anhang: Datei liegt schon da
            if not track.playable:
                raise MediaError("Die angehängte Datei ist nicht mehr vorhanden – bitte neu senden.")
            return
        want_video = self.player.video_enabled and self.cfg.enable_video
        room = None if quiet else (self._fetch_room or self._notice_room())
        if room:
            card = cards.track_card("downloading", track.title or track.key,
                                    **self._track_meta(track))
            eid = await self._put_track_card(room, card, self._card_pending.get(room))
            if eid:
                self._card_pending[room] = eid
        from .ytdl import Resolved
        resolved = Resolved(key=track.key, title=track.title, url=track.url,
                            duration=track.duration, uploader=track.uploader)
        cached = self.store.hot_path(track.key, want_video)    # oft gespielt: liegt schon da
        if cached:
            self.store.set_file(track.key, cached)
            return
        self._fetching += 1
        try:
            path = await self.resolver.download_async(resolved, want_video, self.cfg.tmp_dir)
        finally:
            self._fetching -= 1
        self.store.set_file(track.key, path)
        self.store.note_download(track.key, want_video)
        self.store.prune_hot()

    async def _start_key(self, room_id: str, key: str, sender: str,
                         *, announce: bool = True) -> bool:
        track = self.store.get(key)
        if track is None:
            await self._note(room_id, f"Titel {key} ist nicht (mehr) verfuegbar.", "warning", "⚠️")
            return False
        # Laufendem Element Call im Raum beitreten (kein Durchklingeln).
        if self.ecall.room_id != room_id:
            await self._join_element_call(room_id, force=False)
        try:
            await self.player.play(track, sender)
        except Exception as exc:
            log.exception("Wiedergabe fehlgeschlagen")
            self.store.release_file(track.key)
            await self._fail_pending(room_id, track.title or track.key,
                                     f"Download/Wiedergabe fehlgeschlagen: {exc}")
            return False
        if announce:
            await self._announce(track)
        return True

    async def _join_element_call(self, room_id: str, *, force: bool) -> bool:
        m = self.matrix
        try:
            await self.ecall.load_members(room_id)
            if not force and not self.ecall.others_in_call(room_id):
                await m.send_notice(room_id, "Kein Call aktiv – starte den Anruf, "
                                             "dann !joincall (ich spiele trotzdem schon).")
                return False
            await self.ecall.join(room_id)
            return True
        except Exception as exc:
            log.exception("Element Call beitreten fehlgeschlagen")
            await m.send_notice(room_id, f"Call beitreten fehlgeschlagen: {exc}")
            return False

    # ------------------------------------------------------------------ Playlists
    async def _resolve_for_list(self, room_id: str, query: str):
        """Link/Suche -> gemerkter Titel (nur Metadaten) oder None bei Ablehnung."""
        resolved = await self.resolver.resolve_async(query)
        limit = self.cfg.max_song_seconds
        if limit and resolved.duration > limit:
            await self.matrix.send_notice(
                room_id, f"⛔ Zu lang: {resolved.title} ({fmt_time(resolved.duration)}). "
                         f"Maximal {fmt_time(limit)}.")
            return None
        return self.store.note_request(
            resolved.key, title=resolved.title, url=resolved.url,
            duration=resolved.duration, uploader=resolved.uploader)

    def _title(self, key: str) -> str:
        t = self.store.get(key)
        return (t.title if t else "") or key

    async def _play_keys(self, room_id: str, sender: str, keys: list[str],
                         *, replace: bool) -> None:
        """Titelliste abspielen (replace) oder hinten anhaengen."""
        p = self.player
        if room_id in self._busy:
            await self.matrix.send_notice(room_id, "Ich lade gerade schon etwas herunter …")
            return
        self._busy.add(room_id)
        try:
            self._fetch_room = room_id
            if not replace and p.state != "stopped":
                for key in keys:
                    p.enqueue(key, sender)
                await self.matrix.send_notice(room_id, f"➕ {len(keys)} Titel in der Warteschlange.")
                return
            p.clear_queue()
            for key in keys[1:]:
                p.enqueue(key, sender)
            self._history.append(keys[0])
            del self._history[:-20]
            ok = await self._start_key(room_id, keys[0], sender)
            if not ok and p.queue:
                await p.skip()  # erster Titel kaputt -> mit dem naechsten weiter
        finally:
            self._busy.discard(room_id)

    async def _playlist(self, room_id: str, sender: str, arg: str) -> None:
        m, st = self.matrix, self.store
        sub, _, rest = arg.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub in ("", "list", "liste"):
            rows = st.playlist_list()
            await m.send_notice(room_id, "\n".join(f"• {n} ({c} Titel)" for n, c in rows)
                                or "Noch keine Playlists. Anlegen: !playlist create <name>")
            return
        name, _, extra = rest.partition(" ")
        extra = extra.strip()
        if not name:
            await m.send_notice(room_id, "Name fehlt. Hilfe: !hilfe")
            return

        if sub in ("create", "neu", "erstellen"):
            if not PLAYLIST_NAME.match(name):
                await m.send_notice(room_id, "Name: 1-32 Zeichen, Buchstaben/Ziffern/._- , keine Leerzeichen.")
            elif st.playlist_create(name, sender):
                await m.send_notice(room_id, f"📃 Playlist „{name}“ angelegt. Titel: !playlist add {name} <Link>")
            else:
                await m.send_notice(room_id, f"„{name}“ gibt es schon.")
            return
        keys = st.playlist_keys(name)
        if keys is None:
            await m.send_notice(room_id, f"Playlist „{name}“ gibt es nicht. Liste: !playlist list")
            return

        if sub in ("delete", "loeschen", "löschen"):
            if self._always and self._always.get("name", "").lower() == name.lower():
                self._always = None
            st.playlist_delete(name)
            await m.send_notice(room_id, f"🗑 Playlist „{name}“ gelöscht.")
        elif sub in ("add", "hinzufuegen", "hinzufügen"):
            if extra:
                track = await self._resolve_for_list(room_id, extra)
                if track is None:
                    return
            elif self.player.current is not None:
                track = self.player.current
            else:
                await m.send_notice(room_id, "Benutzung: !playlist add <name> <Link oder Suchbegriff>")
                return
            st.playlist_add(name, track.key)
            await m.send_notice(room_id, f"➕ {track.title or track.key} → „{name}“ ({len(keys) + 1} Titel)")
        elif sub in ("remove", "entfernen"):
            try:
                idx = int(extra)
            except ValueError:
                await m.send_notice(room_id, "Benutzung: !playlist remove <name> <Nummer>")
                return
            key = st.playlist_remove(name, idx)
            await m.send_notice(room_id, f"➖ {self._title(key)} entfernt." if key
                                else f"Keine Nummer {idx} in „{name}“.")
        elif sub in ("show", "zeige", "anzeigen"):
            lines = [f"📃 {name} ({len(keys)} Titel)"]
            lines += [f"{i}. {self._title(k)}" for i, k in enumerate(keys[:MAX_SHOWN], 1)]
            if len(keys) > MAX_SHOWN:
                lines.append(f"… und {len(keys) - MAX_SHOWN} weitere")
            await m.send_notice(room_id, "\n".join(lines))
        elif sub in ("play", "abspielen", "enqueue", "anhaengen", "anhängen"):
            if not keys:
                await m.send_notice(room_id, f"„{name}“ ist leer.")
                return
            keys = list(keys)
            if "shuffle" in extra.lower() or "mischen" in extra.lower():
                random.shuffle(keys)
            await self._play_keys(room_id, sender, keys,
                                  replace=sub in ("play", "abspielen"))
        else:
            await m.send_notice(room_id, "Unbekannt. Benutzung: !playlist create|delete|add|remove|show|list|play|enqueue")

    async def _always_cmd(self, room_id: str, sender: str, arg: str) -> None:
        m = self.matrix
        sub, _, rest = arg.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        cfg = self._always
        if sub == "" and not cfg:
            sub = "radio"                      # nacktes !24/7 startet das Radio
        if sub in ("", "status"):
            if not cfg:
                await m.send_notice(room_id, "24/7 ist aus. !24/7 radio · !24/7 song [Link] · !24/7 playlist <name>")
            elif cfg["kind"] == "radio":
                await m.send_notice(room_id, "📻 24/7 Radio läuft (zufällig aus dem Radio-Ordner). Beenden: !24/7 off")
            elif cfg["kind"] == "playlist":
                await m.send_notice(room_id, f"♾ 24/7 läuft: Playlist „{cfg['name']}“. Beenden: !24/7 off")
            else:
                await m.send_notice(room_id, f"♾ 24/7 läuft: {self._title(cfg['keys'][0])}. Beenden: !24/7 off")
        elif sub in ("off", "aus", "stop"):
            if cfg:
                self._always = None
                await m.send_notice(room_id, "24/7 aus. Der aktuelle Titel läuft zu Ende, "
                                             "danach verlasse ich den Call.")
            else:
                await m.send_notice(room_id, "24/7 war nicht an.")
        elif sub in ("song", "lied", "titel"):
            if rest:
                track = await self._resolve_for_list(room_id, rest)
                if track is None:
                    return
            elif self.player.current is not None:
                track = self.player.current
            else:
                await m.send_notice(room_id, "Benutzung: !24/7 song <Link oder Suchbegriff>")
                return
            self._always = {"kind": "song", "keys": [track.key]}
            self._loop_on = False
            await m.send_notice(room_id, f"♾ 24/7: {track.title or track.key} läuft in Dauerschleife.")
            if rest or self.player.state == "stopped":
                await self._play_keys(room_id, sender, [track.key], replace=True)
        elif sub in ("radio", "random", "zufall"):
            keys = await asyncio.to_thread(self._radio_shuffled)
            if not keys:
                await m.send_notice(room_id, "📻 Der Radio-Ordner ist leer. Lege MP3-Dateien in den Ordner "
                                             "`radio/` neben der compose.yaml und versuch es nochmal.")
                return
            self._always = {"kind": "radio", "keys": []}
            self._loop_on = False
            await m.send_notice(room_id, f"📻 24/7 Radio: {len(keys)} Dateien, zufällige Reihenfolge. Beenden: !24/7 off")
            await self._play_keys(room_id, sender, keys, replace=True)
        elif sub in ("playlist", "pl"):
            keys = self.store.playlist_keys(rest) if rest else None
            if not keys:
                await m.send_notice(room_id, "Benutzung: !24/7 playlist <name> (muss existieren und Titel haben)")
                return
            self._always = {"kind": "playlist", "name": rest, "keys": []}
            self._loop_on = False
            await m.send_notice(room_id, f"♾ 24/7: Playlist „{rest}“ läuft in Dauerschleife.")
            await self._play_keys(room_id, sender, list(keys), replace=True)
        else:
            await m.send_notice(room_id, "Benutzung: !24/7 radio | song [Link] | playlist <name> | off")

    # ------------------------------------------------------------------ Listen
    async def _show_queue(self, room_id: str) -> None:
        rows = self.player.queue
        cur = self.player.current
        items: list[dict] = []
        keys: list[str] = []
        for key, requester in rows:
            t = self.store.get(key)
            keys.append(key)
            item = {"title": (t.title if t else "") or key,
                    "duration": (t.duration if t and t.duration else None)}
            items.append({k: v for k, v in item.items() if v is not None})
        self._listing[room_id] = keys
        now = {"title": cur.title or cur.key} if cur else None
        if now and cur.uploader:
            now["artist"] = cur.uploader
        shown = items[:MAX_SHOWN]
        acts = (cards.action("🗑 Leeren", "!clear"), cards.action("🔀 Mischen", "!shuffle")) if items else ()
        await self.matrix.send_card(room_id, cards.card_content(
            cards.queue_body(now, shown, len(items)),
            cards.queue_card(now, shown, len(items), acts)))

    async def _show_status(self, room_id: str) -> None:
        s = self.player.status()
        t = s["track"]
        state = s["state"] if s["state"] in ("playing", "paused") else "stopped"
        title = (t.title if t else "") or "kein Titel"
        acts = cards.controls(state == "paused", self._loop_on) if state in ("playing", "paused") else ()
        meta = self._track_meta(t) if t else {}
        meta["duration"] = s["duration"] or meta.get("duration", 0.0)
        card = cards.track_card(
            state, title, position=s["position"], seek=cards.SEEK_COMMAND if t else "",
            volume=s["volume"], started_at_ms=now_ms() if state == "playing" else None, actions=acts,
            note=f"Lautstärke {s['volume']} · Video {'an' if s['video'] else 'aus'} · "
                 f"Peers im Call: {s['peers']} · Warteschlange: {len(s['queue'])}", **meta)
        body = (f"{ {'playing': '▶', 'paused': '⏸', 'stopped': '⏹'}[state] } {title} "
                f"({fmt_time(s['position'])} / {fmt_time(s['duration']) if s['duration'] else '?'})\n"
                f"Lautstaerke {s['volume']} · Video {'an' if s['video'] else 'aus'}\n"
                f"Peers im Call: {s['peers']} · Warteschlange: {len(s['queue'])}")
        await self.matrix.send_card(room_id, cards.card_content(body, card))

    # ------------------------------------------------------------------ Dashboard
    def _dashboard_url(self) -> str:
        return self.cfg.public_url or f"http://<server>:{self.cfg.web_port}"

    async def _send_dashboard_login(self, room_id: str, sender: str) -> None:
        if not self.cfg.web_enabled:
            await self._note(room_id, "Das Dashboard ist ausgeschaltet (WEB_ENABLED).", "warning")
            return
        token = self.webauth.issue_link_token(sender, room_id)
        url = f"{self._dashboard_url()}/#login={token}"
        body = (f"🎛 Dein persönlicher Link zum Musikbot-Dashboard:\n{url}\n\n"
                "Einfach öffnen – du bleibst eingeloggt (auch auf mehreren Geräten). "
                "Nicht weitergeben: wer den Link hat, kann den Bot steuern. "
                "Neuer Link (der alte wird ungültig): !dashboard")
        html = (f"<p>🎛 <b>Dein persönlicher Link zum Musikbot-Dashboard</b></p>"
                f"<p><a href=\"{url}\">Dashboard öffnen</a></p>"
                "<p>Einfach öffnen – du bleibst eingeloggt (auch auf mehreren Geräten). "
                "Nicht weitergeben: wer den Link hat, kann den Bot steuern. "
                "Neuer Link (der alte wird ungültig): <code>!dashboard</code></p>")
        who = self._who(sender)
        in_dm = self.matrix.find_dm(sender) == room_id
        if in_dm:
            pass                                    # Befehl kam schon im Privatchat: Link kommt hierher
        elif self.matrix.find_dm(sender):
            await self._note(room_id, f"📬 {who}, schau in deine DMs – dein Dashboard-Link ist da.",
                             "success", "🎛")
        else:
            await self._note(room_id, f"📬 {who}, schau in deine DMs: Ich hab dich in einen Privatchat "
                                      "eingeladen – Einladung annehmen, da steht dein Dashboard-Link.",
                             "success", "🎛")

        async def deliver() -> None:              # im Hintergrund: der neue Privatchat braucht ein paar Sekunden
            if not await self.matrix.send_dm(sender, body, html):
                await self._note(room_id, "Konnte dir keine Direktnachricht schicken – siehe Log.", "error", "❌")

        task = asyncio.create_task(deliver(), name="dashboard-dm")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _load_fade_settings(self) -> None:
        for name in ("crossfade_seconds", "fade_in_seconds"):
            raw = self.store.setting(name)
            if raw:
                try:
                    setattr(self.cfg, name, max(0.0, min(20.0, float(raw))))
                except ValueError:
                    pass

    def _web_room(self, user: str) -> str:
        room = self._notice_room() or self.webauth.room_of(user)
        if not room:
            raise MediaError("Ich weiß nicht, in welchem Raum ich spielen soll – schreib !dashboard "
                             "in den Musik-Raum.")
        return room

    def _track_json(self, key: str, requester: str = "") -> dict:
        t = self.store.get(key)
        return {"key": key, "title": (t.title if t else "") or key,
                "artist": (t.uploader if t else "") or "", "duration": (t.duration if t else 0) or 0,
                "url": (t.url if t else "") or "", "thumb": self._thumb_source(key),
                "requester": self._who(requester) if requester else ""}

    def web_state(self, user: str) -> dict:
        p = self.player
        st = p.status()
        cur = st["track"]
        track = self._track_json(cur.key, p.requester) if cur else None
        if track and st["duration"]:
            track["duration"] = st["duration"]
        hist = [k for k in reversed(self._history) if not cur or k != cur.key][:8]
        always = None
        if self._always:
            always = {"kind": self._always.get("kind"), "name": self._always.get("name", "")}
        return {
            "me": user, "state": st["state"], "position": st["position"], "track": track,
            "volume": st["volume"], "video": st["video"], "loop": self._loop_on, "always": always,
            "queue": [self._track_json(k, r) for k, r in st["queue"]],
            "history": [self._track_json(k) for k in hist],
            "peers": st["peers"], "in_call": bool(self.ecall.active or self.calls.peers),
            "busy": bool(self._fetching or self._busy or p._lock.locked()),
            "crossfade": self.cfg.crossfade_seconds, "fade_in": self.cfg.fade_in_seconds,
            "room": self._notice_room() or self.webauth.room_of(user),
            "favs": self.store.fav_keys(user),
            "me_name": self._names.get(user) or self._who(user),
        }

    async def _display_name(self, user: str) -> str:
        if user not in self._names:
            name = ""
            try:
                resp = await self.matrix.client.get_displayname(user)
                name = getattr(resp, "displayname", "") or ""
            except Exception:
                log.debug("Anzeigename von %s nicht ladbar", user, exc_info=True)
            self._names[user] = name or self._who(user)
        return self._names[user]

    async def web_library(self, user: str) -> dict:
        """Persoenliche Bibliothek: eigene Playlists, Favoriten, Verlauf, Statistik – plus die der anderen."""
        name = await self._display_name(user)
        lists = self.store.playlists_full()
        mine = [p for p in lists if p["owner"].lower() == user.lower()]
        others = [p for p in lists if p["owner"].lower() != user.lower()]
        for p in others:
            p["by"] = self._who(p["owner"]) if p["owner"] else ""
        for p in mine:
            p["tracks"] = [self._track_json(k) for k in self.store.playlist_keys(p["name"]) or []]
        totals = self.store.totals_of(user)
        return {
            "me": {"id": user, "name": name, **totals},
            "my_playlists": mine,
            "playlists": others,
            "favorites": [self._track_json(k) for k in self.store.fav_keys(user)],
            "my_top": [dict(self._track_json(k), plays=n) for k, n in self.store.top_of(user, 10)],
            "my_recent": [self._track_json(k) for k in self.store.recent_of(user, 10)],
            "top": [dict(self._track_json(k), plays=n) for k, n in self.store.top_tracks(10)],
            "radio": len(self._radio_scan()) if os.path.isdir(self.cfg.radio_dir) else 0,
        }

    async def web_playlist(self, user: str, op: str, name: str, key: str = "", index: int = 0,
                           query: str = "") -> None:
        """Eigene Playlists im Dashboard verwalten (fremde nur abspielen)."""
        name = name.strip()[:60]
        if not name:
            raise MediaError("Bitte einen Namen angeben.")
        st = self.store
        if op == "create":
            if not st.playlist_create(name, user):
                raise MediaError(f"Eine Playlist „{name}“ gibt es schon.")
            return
        owner = st.playlist_owner(name)
        if owner is None:
            raise MediaError(f"Playlist „{name}“ gibt es nicht.")
        if owner and owner.lower() != user.lower():
            raise MediaError("Das ist nicht deine Playlist – die darf nur ihr Besitzer ändern.")
        if op == "add":
            if not key and query:                       # Suchtreffer: erst Metadaten holen
                resolved = await self.resolver.resolve_async(query)
                key = st.note_request(resolved.key, title=resolved.title, url=resolved.url,
                                      duration=resolved.duration, uploader=resolved.uploader).key
                if resolved.thumbnail:
                    self._thumb_url[resolved.key] = resolved.thumbnail
            cur = self.player.current
            key = key or (cur.key if cur else "")
            if not key or st.get(key) is None:
                raise MediaError("Welcher Titel? Gerade läuft nichts.")
            st.playlist_add(name, key)
        elif op == "remove":
            if st.playlist_remove(name, index + 1) is None:
                raise MediaError("Diesen Titel gibt es in der Playlist nicht (mehr).")
        elif op == "delete":
            st.playlist_delete(name)
        else:
            raise MediaError("Unbekannte Aktion.")

    # Erlaubte Befehle aus dem Dashboard (wie im Chat, gleiche Karten im Raum)
    _WEB_COMMANDS = {"play", "pause", "resume", "stop", "skip", "previous", "clear", "seek",
                     "forward", "rewind", "volume", "video", "shuffle", "loop", "joincall",
                     "leavecall", "always", "playlist", "fav"}

    async def web_command(self, user: str, name: str, arg: str) -> None:
        if name == "softskip":
            await self._quick(self._soft_skip(self._web_room(user)))
            return
        if name == "softstop":
            await self._quick(self._soft_stop(self._web_room(user), float(arg or 4)))
            return
        if name == "favkey":                          # beliebigen Titel merken/entfernen
            keys = self.store.fav_keys(user)
            if arg in keys:
                self.store.fav_remove(user, keys.index(arg) + 1)
            elif self.store.get(arg):
                self.store.fav_add(user, arg)
            return
        if name not in self._WEB_COMMANDS:
            raise MediaError("Unbekannter Befehl.")
        if name == "playlist" and arg.split(" ", 1)[0] not in ("play", "enqueue"):
            raise MediaError("Playlists bitte im Chat bearbeiten.")
        await self._quick(self._dispatch(self._web_room(user), user, name, arg))

    async def _quick(self, coro, wait: float = 0.35) -> None:
        """Befehl starten und kurz warten: Der Player reagiert sofort, die Karten im Chat
        (Netzwerk) laufen im Hintergrund weiter – so fuehlt sich das Dashboard flott an."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or not t.exception() or log.error(
            "Dashboard-Befehl fehlgeschlagen", exc_info=t.exception()))
        done, _ = await asyncio.wait({task}, timeout=wait)
        if done:
            task.result()                        # Fehler (z. B. MediaError) gehen ans Dashboard

    async def _soft_skip(self, room: str) -> None:
        p = self.player
        self._loop_on = False
        if self._always and not p.queue:
            self._refill_always()
        if await p.crossfade_now():
            return
        if p.queue and not await p.fade_out(1.5):
            return
        await self._dispatch(room, "", "skip", "")
        p.reset_master()

    async def _soft_stop(self, room: str, seconds: float) -> None:
        if await self.player.fade_out(max(0.5, min(15.0, seconds))):
            await self._dispatch(room, "", "stop", "")
        self.player.reset_master()

    async def web_add(self, user: str, query: str, mode: str) -> None:
        if not query.strip():
            raise MediaError("Bitte Link oder Suchbegriff angeben.")
        room = self._web_room(user)
        resolved = await self.resolver.resolve_async(query)
        limit = self.cfg.max_song_seconds
        if limit and resolved.duration > limit:
            raise MediaError(f"Zu lang ({fmt_time(resolved.duration)}). Maximal {fmt_time(limit)}.")
        track = self.store.note_request(resolved.key, title=resolved.title, url=resolved.url,
                                        duration=resolved.duration, uploader=resolved.uploader)
        if resolved.thumbnail:
            self._thumb_url[resolved.key] = resolved.thumbnail
        self._prefetch_thumb(resolved.key)
        p = self.player
        idle = p.state == "stopped" and not p._lock.locked()
        if idle:
            self._fetch_room = room
            self._history.append(track.key)
            del self._history[:-20]
            await self._start_key(room, track.key, user, announce=True)
        elif mode in ("now", "next"):
            p.queue.insert(0, (track.key, user))
            if mode == "now":
                await self._soft_skip(room)
        else:
            p.enqueue(track.key, user)

    def web_queue(self, user: str, op: str, index: int, to: int) -> None:
        q = self.player.queue
        if not (0 <= index < len(q)):
            raise MediaError("Diesen Platz gibt es nicht (mehr).")
        if op == "remove":
            q.pop(index)
        elif op == "top":
            q.insert(0, q.pop(index))
        elif op == "move" and 0 <= to < len(q):
            q.insert(to, q.pop(index))
        else:
            raise MediaError("Unbekannte Aktion.")

    async def web_fade(self, user: str, data: dict) -> None:
        for field, name in (("crossfade", "crossfade_seconds"), ("fade_in", "fade_in_seconds")):
            if field in data:
                value = max(0.0, min(20.0, float(data[field])))
                setattr(self.cfg, name, value)
                self.store.set_setting(name, str(value))
