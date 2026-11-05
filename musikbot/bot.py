"""Musikbot fuer klassische Matrix-Calls.

Verdrahtet: Matrix (nio) -> Befehle -> yt-dlp -> Speicher-Policy -> Player
-> WebRTC-Peers. Der Bot ist ein normaler Matrix-Nutzer und betritt Calls
ueber das Telefon-Icon (Peer-zu-Peer/mesh, klassisches m.call-Signaling).
"""
from __future__ import annotations

import logging
import random

from .calls import CallManager
from .commands import HELP, parse
from .config import Config
from .hub import Hub
from .matrix import Matrix
from .player import Player
from .policy import LIBRARY, Policy
from .store import TrackStore
from .util import fmt_time, parse_time
from .ytdl import MediaError, MediaResolver

log = logging.getLogger("musikbot")


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = TrackStore(cfg.db_path, Policy.from_config(cfg),
                                cfg.cache_dir, cfg.library_dir)
        self.hub = Hub()
        self.player = Player(cfg, self.hub, self.store,
                             on_event=self._player_event)
        self.calls = CallManager(cfg, self.player)
        self.matrix = Matrix(cfg)
        self.resolver = MediaResolver(cfg)
        # Buchhaltung, welcher Raum welche Liste zuletzt gesehen hat:
        self._listing: dict[str, list[str]] = {}
        self._history: list[str] = []
        self._loop_on = False
        self._busy: set[str] = set()

        m = self.matrix
        m.on_text = self._on_text
        m.on_call = self.calls.handle_event
        m.on_member_join = self.calls.on_member_joined
        m.on_room_left = self.calls.on_room_left
        self.calls.send_room_event = m.send_room_event
        self.calls.send_to_device = m.send_to_device
        self.calls.devices_of_room = m.devices_of_room

    # ------------------------------------------------------------------ Start
    async def startup(self) -> list[str]:
        await self.matrix.login()
        rooms = await self.matrix.prepare_rooms()
        for rid in rooms:
            await self.matrix.ensure_room_state(rid)
        turn = await self.matrix.fetch_turn()
        if turn:
            self.calls.set_turn(turn)
        return rooms

    async def run(self) -> None:
        rooms = await self.startup()
        log.info("Bot bereit. Raeume: %s", ", ".join(rooms) or "(keine konfiguriert)")
        await self.matrix.run()

    async def shutdown(self) -> None:
        await self.calls.hangup_all("shutdown")
        await self.player.shutdown()
        self.matrix.stop()
        self.store.close()

    # ------------------------------------------------------------------ Events
    async def _player_event(self, name: str, data: dict) -> None:
        room = self.calls.active_room
        if not room:
            return
        if name == "track-end":
            if self._loop_on and data.get("track") is not None:
                self.player.enqueue(data["track"].key, "")
        elif name == "advanced":
            track = data.get("track")
            await self._announce(track)
        elif name == "missing":
            key = data.get("key", "")
            await self.matrix.send_notice(room, f"⚠️ Titel ({key}) ist nicht mehr verfuegbar.")
        elif name == "queue-empty":
            if not data.get("auto"):
                return
            await self.matrix.send_notice(room, "Warteschlange ist leer – ich pausiere.")

    async def _announce(self, track) -> None:
        room = self.calls.active_room
        if not room or track is None:
            return
        dur = fmt_time(track.duration)
        pos = f" [{dur}]" if dur else ""
        await self.matrix.send_notice(room, f"🎵 {(track.title or track.key)}{pos}")

    # ------------------------------------------------------------------ Text
    async def _on_text(self, room_id: str, sender: str, body: str) -> None:
        cmd = parse(body, self.cfg.prefixes)
        if cmd is None:
            return
        if not self._allowed(sender):
            await self.matrix.send_notice(room_id, "Das darfst du hier nicht.")
            return
        try:
            await self._dispatch(room_id, sender, cmd.name, cmd.arg)
        except MediaError as exc:
            await self.matrix.send_notice(room_id, f"❌ {(str(exc))}")
        except Exception:
            log.exception("Command %s fehlgeschlagen", cmd.name)
            await self.matrix.send_notice(room_id, "Interner Fehler – siehe Log.")

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
                await m.send_notice(room_id, "Benutzung: /play <Link oder Suchbegriff>")
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
            await m.send_notice(room_id, "⏸ pausiert." if ok else "Lauft gerade nicht.")
        elif name == "resume":
            ok = p.resume()
            await m.send_notice(room_id, "▶weiter." if ok else "Nichts pausiert.")
        elif name == "stop":
            await p.stop()
            await m.send_notice(room_id, "⏹ gestoppt.")
        elif name == "skip":
            if self._loop_on:
                self._loop_on = False
            ok = await p.skip()
            await m.send_notice(room_id, "⏭ naechster Titel." if ok else "Warteschlange ist leer.")
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
            await m.send_notice(room_id, f"Warteschlange geleert ({n} Titel).")
        elif name == "status":
            await self._show_status(room_id)
        elif name == "seek":
            seconds = parse_time(arg)
            if seconds is None:
                await m.send_notice(room_id, "Benutzung: /seek <mm:ss oder Sekunden>")
                return
            ok = await p.seek(seconds)
            await m.send_notice(room_id, f"⏩ {fmt_time(p.position)}" if ok else "Kann gerade nicht gespult werden.")
        elif name == "volume":
            try:
                value = int(arg.strip())
            except ValueError:
                await m.send_notice(room_id, f"Lautstaerke ist {p.volume}/100. Benutzung: /volume 0-100")
                return
            v = p.set_volume(value)
            await m.send_notice(room_id, f"🔊 Lautstaerke: {v}")
        elif name == "video":
            mode = arg.strip().lower()
            on = p.set_video(True if mode == "on" else False if mode == "off" else None)
            await m.send_notice(room_id, f"Video: {'an' if on else 'aus'}")
        elif name == "shuffle":
            random.shuffle(p.queue)
            await m.send_notice(room_id, "Warteschlage gemischt." if p.queue else "Warteschlange ist leer.")
        elif name == "loop":
            mode = arg.strip().lower()
            self._loop_on = True if mode == "on" else False if mode == "off" else not self._loop_on
            await m.send_notice(room_id, f"Schleife: {'an' if self._loop_on else 'aus'}")
        elif name == "joincall":
            await self.matrix.ensure_room_state(room_id)
            await self.calls.start_call_in_room(room_id)
            await m.send_notice(room_id, "Ich rufe die Mitglieder des Raums an (Mesh).")
        elif name == "leavecall":
            await self.calls.hangup_all("user")
            await m.send_notice(room_id, "Anruf beendet.")
        elif name == "cache":
            await self._show_cache(room_id)
        elif name == "library":
            await self._show_library(room_id)
        elif name == "unsave":
            await self._unsave(room_id, arg)
        elif name == "help":
            await m.send_notice(room_id, HELP)
        else:  # pragma: no cover
            await m.send_notice(room_id, "Unbekanntes Kommando. /help")

    # ------------------------------------------------------------------ Play
    async def _play(self, room_id: str, sender: str, query: str) -> None:
        m = self.matrix
        await m.send_notice(room_id, f"🔎 Suchen: {(query[:80])}")
        resolved = await self.resolver.resolve_async(query)
        await self._book_and_play(room_id, sender, resolved, query)

    async def _book_and_play(self, room_id: str, sender: str, resolved,
                             query: str) -> None:
        m = self.matrix
        st = self.store
        track, outcome = st.note_request(
            resolved.key, title=resolved.title, url=resolved.url,
            duration=resolved.duration, uploader=resolved.uploader)

        if not (track.playable):
            want_video = self.player.video_enabled and self.cfg.enable_video
            dest = st.dir_for(resolved.key)
            await m.send_notice(room_id,
                                f"⬇️ Lade herunter: {(resolved.title)} "
                                f"({fmt_time(resolved.duration)})")
            path = await self.resolver.download_async(resolved, want_video, dest)
            st.set_file(resolved.key, path)
            track = st.get(resolved.key) or track

        if outcome.promoted:
            await m.send_notice(room_id,
                                f"💾 dauerhaft gespeichert ({self.cfg.save_after_requests}. Anforderung): "
                                f"{(track.title or track.key)}")
        if outcome.evicted:
            names = [t.title or t.key for t in outcome.removed]
            await m.send_notice(
                room_id,
                "🧹 aus dem Cache geflogen: " + ", ".join((n) for n in names))

        if self._loop_on and self.player.current and self.player.current.key == track.key:
            self.player.enqueue(track.key, sender)
            await m.send_notice(room_id, "Wiederholung eingeplant.")
            return

        self._history.append(track.key)
        del self._history[:-20]
        await self._start_key(room_id, track.key, sender, announce=True)

    async def _start_key(self, room_id: str, key: str, sender: str,
                         *, announce: bool = True) -> None:
        track = self.store.get(key)
        if track is None or not track.playable:
            await self.matrix.send_notice(room_id, f"Titel {(key)} ist nicht (mehr) verfuegbar.")
            return
        # Call aufbauen, falls noch keiner laeuft.
        if self.calls.active_room != room_id:
            await self.matrix.ensure_room_state(room_id)
            await self.calls.start_call_in_room(room_id)
            self.calls.active_room = room_id
        await self.player.play(track, sender)
        if announce:
            await self._announce(track)

    # ------------------------------------------------------------------ Listen
    async def _show_queue(self, room_id: str) -> None:
        rows = self.player.queue
        cur = self.player.current
        lines: list[str] = []
        if cur:
            lines.append(f"Laeuft: {(cur.title or cur.key)}")
        keys: list[str] = []
        for i, (key, requester) in enumerate(rows, start=1):
            t = self.store.get(key)
            keys.append(key)
            who = f" (von {requester})" if requester else ""
            lines.append(f"{i}. {((t.title if t else '') or key)}{who}")
        self._listing[room_id] = keys
        await self.matrix.send_notice(
            room_id, "\n".join(lines) if lines else "Warteschlange ist leer.")

    async def _show_status(self, room_id: str) -> None:
        s = self.player.status()
        t = s["track"]
        dur = fmt_time(s["duration"]) if s["duration"] else "?"
        pos = fmt_time(s["position"])
        state = {"playing": "▶", "paused": "⏸", "stopped": "⏹"}[s["state"]]
        lines = [
            f"{state} {((t.title if t else '') or 'kein Titel')} "
            f"({pos} / {dur})",
            f"Lautstaerke {s['volume']}/100 · Video {'an' if s['video'] else 'aus'}",
            f"Peers im Call: {s['peers']} · Warteschlange: {len(s['queue'])}",
        ]
        await self.matrix.send_notice(room_id, "\n".join(lines))

    async def _show_cache(self, room_id: str) -> None:
        rows = self.store.cache_report()
        kb = self.store.cache_used_bytes() // 1024
        lines = [f"Cache: {len(rows)}/{self.cfg.cache_max_entries} Titel, {kb} KiB"]
        keys = []
        for i, (track, status) in enumerate(rows, start=1):
            keys.append(track.key)
            n = self.store.get(track.key)
            lines.append(f"{i}. {(track.title or track.key)} – {status} "
                         f"({n.requests if n else 0}x angefordert)")
        self._listing[room_id] = keys
        if not rows:
            lines.append("(leer)")
        await self.matrix.send_notice(room_id, "\n".join(lines))

    async def _show_library(self, room_id: str) -> None:
        rows = self.store.by_state(LIBRARY)
        kb = self.store.library_used_bytes() // 1024
        lines = [f"Bibliothek: {len(rows)} Titel, {kb} KiB"]
        keys = []
        for i, track in enumerate(rows, start=1):
            keys.append(track.key)
            lines.append(f"{i}. {(track.title or track.key)} "
                         f"({track.requests}x angefordert)")
        self._listing[room_id] = keys
        if not rows:
            lines.append("(leer) – Titel werden ab "
                         f"{self.cfg.save_after_requests} Anforderungen gespeichert")
        await self.matrix.send_notice(room_id, "\n".join(lines))

    async def _unsave(self, room_id: str, arg: str) -> None:
        keys = self._listing.get(room_id, [])
        try:
            idx = int(arg.strip())
        except ValueError:
            await self.matrix.send_notice(room_id, "Benutzung: /unsave <Nummer> (zuvor /library oder /cache)")
            return
        if not (1 <= idx <= len(keys)):
            await self.matrix.send_notice(room_id, "Diese Nummer kenne ich nicht – bitte erst /library.")
            return
        key = keys[idx - 1]
        ok = self.store.forget(key)
        await self.matrix.send_notice(room_id,
                                      "Entfernt." if ok else "Nichts zu entfernen.")
