"""Wiedergabe: Warteschlange, Mix-Loop, Pause/Resume/Seek/Lautstärke.

Der Mix-Loop ist die einzige Stelle, die dekodiert. Er hält die Audio-Position
an der Wanduhr, schiebt Blöcke in den Hub und treibt das Video in einem
eigenen, fps-begrenzten Takt hinterher.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Awaitable, Callable, Iterator, Optional

import numpy as np
from aiortc import MediaStreamTrack
from av import AudioFrame, VideoFrame

from .ansage import Ansager, spoken_title, text_for
from .baer import BaerDJ
from .config import Config
from .hub import Hub, Sink
from .media import MediaSource, apply_gain, crossfade_mix, fade_block, silence_block
from .moderation import Moderator
from .store import Track, TrackStore

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
LEAD_SECONDS = 0.4  # Puffer, damit kleiner Jitter im Call nicht stört
PREFETCH_SECONDS = 45.0  # so lange vor der Ueberblendung wird der naechste Titel geladen
NEED_NEXT_SECONDS = 30.0  # noch frueher: "need-next" (z. B. Autoplay sucht einen aehnlichen Titel)
AUTOPLAY = "autoplay"     # Wunsch-Absender fuer Titel, die Autoplay eingereiht hat
CUT_FADE = 0.3  # Skip/Stop/Seek: kurz ausblenden statt hart abschneiden (kein Knacken)
EventHandler = Callable[[str, dict], Awaitable[None]]


MAX_VOLUME = 300  # 60 = Originalpegel, darueber Boost (300 = 5x)
DUCK = 0.3              # Musik waehrend der Ansage
ANSAGE_LATEST = 8.0     # spaeter als so viele Sekunden im Titel keine Ansage mehr
FRESH_START = 15 * 60   # so lange nichts gespielt -> beim naechsten Titel "Hallo zusammen"


class PeerAudioTrack(MediaStreamTrack):
    """WebRTC-Audio eines Teilnehmers: holt Blöcke aus dessen Sink."""

    kind = "audio"

    def __init__(self, sink: Sink) -> None:
        super().__init__()
        self._sink = sink
        self._pts = 0
        self._anchor: float | None = None

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        if self._anchor is None:
            self._anchor = loop.time()
        block = self._sink.take_audio()
        if block is None:
            block = silence_block()
        samples = block.shape[1] // 2
        frame = AudioFrame.from_ndarray(block, format="s16", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._pts += samples
        self._anchor += samples / SAMPLE_RATE
        delay = self._anchor - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return frame


class PeerVideoTrack(MediaStreamTrack):
    """WebRTC-Video eines Teilnehmers; bei Pause wird das letzte Bild wiederholt."""

    kind = "video"

    def __init__(self, sink: Sink, fps: float) -> None:
        super().__init__()
        self._sink = sink
        self._fps = max(1.0, fps)
        self._pts = 0
        self._anchor: float | None = None
        self._last: VideoFrame | None = None

    async def recv(self) -> VideoFrame:
        loop = asyncio.get_running_loop()
        if self._anchor is None:
            self._anchor = loop.time()
        frame = self._sink.take_video()
        if frame is None:
            frame = self._last.copy() if self._last is not None else _blank_frame()
        else:
            self._last = frame
        self._anchor += 1.0 / self._fps
        delay = self._anchor - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        # pts fortlaufend setzen, damit Springen (Seek/Überholen) nicht stört
        frame.pts = self._pts
        frame.time_base = Fraction(1, 1000)
        self._pts += int(round(1000 / self._fps))
        return frame


@dataclass
class _Prepared:
    """Vorgeladener naechster Titel (Datei da, Decoder offen)."""

    key: str
    track: Track
    source: MediaSource


@dataclass
class _Fade:
    """Ausblendender alter Titel waehrend einer Ueberblendung."""

    track: Track
    source: MediaSource
    audio_iter: Optional[Iterator]
    total: int        # Laenge der Ueberblendung in Samples
    done: int = 0


def _blank_frame() -> VideoFrame:
    arr = np.zeros((16, 16, 3), dtype=np.uint8)
    return VideoFrame.from_ndarray(arr, format="bgr24")


class Player:
    def __init__(self, cfg: Config, hub: Hub, store: TrackStore,
                 *, on_event: EventHandler | None = None,
                 fetch: "Callable[[Track], Awaitable[None]] | None" = None) -> None:
        self.cfg = cfg
        self.hub = hub
        self.store = store
        self.on_event = on_event
        self.fetch = fetch  # lädt die Datei frisch herunter (nie aus einem Cache)
        self.state = "stopped"  # stopped | playing | paused
        self.current: Track | None = None
        self.requester: str = ""
        self.volume = cfg.default_volume
        self.video_enabled = cfg.enable_video
        self.queue: list[tuple[str, str]] = []
        self._source: MediaSource | None = None
        self._audio_iter = None
        self._video_iter = None
        self.cover: tuple[str, VideoFrame] | None = None   # Titelbild als Kamerabild (ohne Video)
        self._pos = 0.0
        self._vpos = 0.0
        self._wall_base = 0.0
        self._mix_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Crossfade: naechster Titel wird vorgeladen und am Ende uebergeblendet
        self._prepared: _Prepared | None = None
        self._prefetch_key: str | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._fade: _Fade | None = None
        self._force_fade = False       # "Sanft ueberspringen": Ueberblendung sofort starten
        self._need_next_for: str | None = None   # fuer welchen Titel "need-next" schon gemeldet wurde
        self._bg: set[asyncio.Task] = set()
        # Master-Lautstaerke fuer Ein-/Ausblenden (0..1), aendert sich je Sample um _master_rate
        self._master = 1.0
        self._master_target = 1.0
        self._master_rate = 0.0
        # Baer-DJ (Kamerabild ohne eigenes Video) und Ansage
        self.baer: BaerDJ | None = None
        if cfg.baer_enabled:
            h = int(cfg.video_height) // 2 * 2
            self.baer = BaerDJ((h * 16 // 9) // 2 * 2, h, cfg.baer_assets or None)
        self.ansager = Ansager(cfg.ansage_voice, {cfg.dj_name: cfg.dj_name_spoken}) \
            if cfg.ansage_enabled else None
        self.moderator = Moderator(cfg.moderation_tz, dj=cfg.dj_name)
        self._gen = 0                  # zaehlt hoch bei play/stop/skip: laufende Moderation bricht ab
        self._no_intro_key: str | None = None   # in der Pause schon angesagt
        self._speaking = False                  # Moderation laeuft (Baer rendert ohne Quelle)
        self._last_start = 0.0
        self._voice: np.ndarray | None = None   # laufende Ansage (Bloecke wie im Mix)
        self._voice_i = 0
        self._baer_task: asyncio.Task | None = None
        self._zuruf_task: asyncio.Task | None = None
        self._listeners: list[str] = []          # Namen der Leute im Call (ohne Bot)
        self._last_speech = 0.0

    # ------------------------------------------------------------ Peers (WebRTC)
    def add_peer_audio(self, peer) -> PeerAudioTrack:
        """Eigener Sink pro Peer: Decoder/Mixer einmal, Encoder pro Geraet."""
        sink = self.hub.new_sink()
        peer.sink = sink
        return PeerAudioTrack(sink)

    def add_peer_video(self, peer) -> Optional[PeerVideoTrack]:
        if not self.video_enabled:
            return None
        sink = getattr(peer, "sink", None) or self.hub.new_sink()
        peer.sink = sink
        return PeerVideoTrack(sink, self.cfg.video_fps)

    def remove_peer(self, peer) -> None:
        sink = getattr(peer, "sink", None)
        if sink is not None:
            self.hub.drop_sink(sink)
            peer.sink = None

    # ------------------------------------------------------------ Mix-Loop
    async def _ensure_loop(self) -> None:
        if self._mix_task is None or self._mix_task.done():
            self._mix_task = asyncio.create_task(self._mix(), name="mix-loop")
        if self.baer is not None and (self._baer_task is None or self._baer_task.done()):
            self._baer_task = asyncio.create_task(self._baer_loop(), name="baer")
        if (self.ansager is not None and self.cfg.zuruf_enabled
                and (self._zuruf_task is None or self._zuruf_task.done())):
            self._zuruf_task = asyncio.create_task(self._zuruf_loop(), name="zuruf")

    async def _mix(self) -> None:
        loop = asyncio.get_running_loop()
        log.debug("Mix-Loop startet")
        try:
            while True:
                if self.state != "playing" or self._source is None:
                    self._wall_base = loop.time() - self._pos
                    await asyncio.sleep(0.05)
                    continue
                self._maybe_prefetch()
                if self._crossfade_due():
                    self._start_crossfade()
                    continue
                target = loop.time() - self._wall_base
                duration = self._source.duration
                exhausted = self._audio_iter is None or (
                    duration is not None and self._pos > duration + 1.0)
                if exhausted:
                    log.debug("Ende des Titels erreicht (pos=%.1f dur=%s)", self._pos, duration)
                    await self._on_track_end()
                    continue
                if self._pos < target + LEAD_SECONDS:
                    it = self._audio_iter
                    try:
                        block = await _next_async(it)
                    except _Exhausted:
                        if it is not self._audio_iter:   # inzwischen gestoppt/gespult/gewechselt
                            continue
                        await self._on_track_end()
                        continue
                    except Exception:  # noqa: BLE001
                        log.exception("Dekoder abgestürzt")
                        await self._on_track_end()
                        continue
                    if it is not self._audio_iter:            # Block gehoert zum alten Titel
                        continue
                    if self._fade is not None:
                        block = await self._mix_fade(block)
                    else:
                        block = apply_gain(block, self.gain)
                    if self.baer is not None:          # Beat aus der Musik (ohne Ansage)
                        self.baer.beat.feed(block, self._wall_base + self._pos)
                    if self._voice is not None:
                        block = self._mix_voice(block)
                    self.hub.push_audio(self._apply_master(block))
                    self._pos += (block.shape[1] // 2) / SAMPLE_RATE
                    continue

                if self.video_enabled and self._video_iter is not None and self._vpos <= self._pos:
                    try:
                        when, frame = await _next_async(self._video_iter)
                    except _Exhausted:
                        self._video_iter = None
                    except Exception:  # noqa: BLE001
                        log.exception("Video-Dekoder abgestürzt")
                        self._video_iter = None
                    else:
                        self.hub.push_video(frame)
                        self._vpos = when + 1.0 / self.cfg.video_fps
                        continue

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            log.debug("Mix-Loop beendet")
            raise

    # -------------------------------------------------------- Wiedergabe
    @property
    def gain(self) -> float:
        return self.volume / 60.0

    async def play(self, track: Track, requester: str = "") -> None:
        """Startet sofort neu (aktiver Titel wird abgelöst)."""
        self._gen += 1
        async with self._lock:
            prepared = await self._take_prepared(track.key)
            # Nur abschneiden, wenn noch etwas laeuft – nach einem natuerlichen Ende
            # soll der Rest im Puffer (das echte Songende) ausklingen.
            await self._teardown(cut=self._source is not None)
            self.current = track
            self.requester = requester
            if prepared is not None:     # schon vorgeladen (Crossfade-Vorbereitung)
                source = prepared.source
            else:
                if self.fetch is not None:
                    await self.fetch(track)  # immer frisch herunterladen
                source = self._new_source(track)
                await asyncio.to_thread(source.open)
            self._source = source
            self._start_streams(0.0)
            self._on_new_track()
            if self.cfg.fade_in_seconds > 0:      # sanft einblenden statt mit voller Lautstaerke
                self._master = 0.0
                self._ramp(1.0, self.cfg.fade_in_seconds)
            else:
                self._master = self._master_target = 1.0
            self.state = "playing"
            await self._ensure_loop()
            log.info("Spielt: %s (%s)", track.title or track.key, track.abs_path)

    def _start_streams(self, start: float) -> None:
        assert self._source is not None
        self._audio_iter = self._source.audio_blocks(start)
        self._video_iter = (
            self._source.video_frames(start)
            if self._shows_video(self._source) else None
        )
        self._pos = start
        self._vpos = start
        # Was noch im Puffer liegt (Ende des alten Titels / kurzes Ausblenden),
        # spielt zuerst – der neue Titel schliesst lueckenlos daran an.
        kept = self.hub.buffered_audio_seconds()
        self._wall_base = asyncio.get_running_loop().time() + kept - start
        self._push_cover()

    def set_cover(self, key: str, frame: VideoFrame | None) -> None:
        """Titelbild fuer `key` als Kamerabild merken (und zeigen, wenn der Titel kein Video hat)."""
        self.cover = (key, frame) if frame is not None else None
        if self.baer is not None and self.current is not None and self.current.key == key:
            self.baer.set_track(self.baer.title, self.baer.artist, frame)
        self._push_cover()

    @property
    def cover_frame(self) -> VideoFrame | None:
        cur = self.current
        if self.cover and cur and self.cover[0] == cur.key:
            return self.cover[1]
        return getattr(self._source, "still", None)   # Cover aus der Datei selbst

    def _push_cover(self) -> None:
        """Ohne eigenes Video bleibt die Kamera nicht schwarz: Titelbild (sonst ein schwarzes Bild)."""
        if not self.video_enabled or self._source is None or self._shows_video(self._source):
            return
        if self._baer_active():
            return                       # der Baer liefert die Bilder
        self.hub.push_video(self.cover_frame or _blank_frame())

    def pause(self) -> bool:
        if self.state != "playing":
            return False
        self.state = "paused"
        return True

    def resume(self) -> bool:
        if self.state != "paused":
            return False
        self.state = "playing"
        self._wall_base = asyncio.get_running_loop().time() - self._pos
        return True

    def toggle(self) -> str:
        if self.state == "playing":
            self.pause()
        elif self.state == "paused":
            self.resume()
        return self.state

    async def stop(self, *, clear_queue: bool = False) -> None:
        self._gen += 1
        async with self._lock:
            if clear_queue:
                self.queue.clear()
            self._discard_prepared()
            await self._teardown()
            self.reset_master()
            self.state = "stopped"

    async def skip(self) -> bool:
        """Aktuellen Titel beenden und nächsten starten."""
        self._gen += 1
        async with self._lock:
            await self._teardown()
        return await self._advance(auto=False)

    async def seek(self, seconds: float) -> bool:
        if self._source is None:
            return False
        seconds = max(0.0, seconds)
        await self._end_fade()
        await asyncio.to_thread(self._source.seek, seconds)
        self.hub.fade_out(CUT_FADE)
        self._voice = None
        self._start_streams(seconds)
        if self.state == "paused":
            self.state = "playing"
        return True

    def set_volume(self, value: int) -> int:
        self.volume = max(0, min(MAX_VOLUME, value))
        return self.volume

    def set_audience(self, faces: list, listeners: list[str] | None = None) -> None:
        """Call-Teilnehmer [(Name, Avatar|None)] fuer den Hintergrund des Baer-Bilds;
        `listeners`: Namen, die der Baer zurufen darf (ohne ihn selbst)."""
        self._listeners = list(listeners or [])
        if self.baer is not None:
            self.baer.set_audience(faces)

    # ------------------------------------------------------------ Zurufe
    def _zuruf_ok(self) -> bool:
        """Darf der Baer jetzt reinrufen? Nur mitten in einem laufenden Song, ohne andere Ansage."""
        if self.ansager is None or not self.ansager.available or self.state != "playing":
            return False
        if self._voice is not None or self._speaking or self.hub.count == 0:
            return False
        if self._source is None or self._pos < 20.0:
            return False
        dur = self._source.duration
        return dur is None or dur - self._pos > 15.0

    @property
    def speech_idle(self) -> float:
        """Sekunden seit der letzten Ansage/dem letzten Zuruf."""
        return asyncio.get_running_loop().time() - self._last_speech

    def _zuruf_gap(self) -> float:
        return self.cfg.zuruf_every * random.uniform(0.6, 1.4)

    async def _zuruf_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time() + self._zuruf_gap()
        last_drop = None
        build_since = None
        while True:
            await asyncio.sleep(0.5)
            now = loop.time()
            drop = build = False
            vibe = None
            if self.baer is not None:
                vibe = self.baer.beat.state(now)
                vibe["bpm"] = self.baer.clock.song_bpm or vibe["bpm"]
                d = vibe["drop_at"]
                if d is not None and d != last_drop:
                    last_drop = d
                    drop = now - d < 1.5 and now - self._last_speech > 20
                # Build-up, das sich ein paar Sekunden haelt: ankuendigen ("Gleich kommt's!")
                build_since = (build_since or now) if vibe["section"] == "build" else None
                build = build_since is not None and now - build_since >= 1.5 and now - self._last_speech > 25
            if (drop or build or (now >= next_at and now - self._last_speech > 30)) and self._zuruf_ok():
                song = self._info(self.current) if self.current else ("", "")
                await self.shout(self.moderator.zuruf(self._listeners, drop=drop, vibe=vibe, song=song))
                next_at = loop.time() + self._zuruf_gap()
                build_since = None

    async def shout(self, text: str) -> bool:
        """Zuruf ueber die laufende Musik legen (Musik kurz leiser, Baer redet)."""
        if not self._zuruf_ok():
            return False
        key = self.current.key if self.current else None
        voice = await asyncio.to_thread(self.ansager.synth, text) if self.ansager else None
        if voice is None or not self._zuruf_ok() or self.current is None or self.current.key != key:
            return False
        self._voice, self._voice_i = voice, 0
        self._last_speech = asyncio.get_running_loop().time()
        log.info("Zuruf: %s", text)
        if self.baer is not None:
            self.baer.announce(self._wall_base + self._pos, voice, headline=f"{self.cfg.dj_name}:", title=text, artist="",
                               kind="shout")
        return True

    def set_video(self, enabled: bool | None = None) -> bool:
        """Video an/aus; beim Einschalten wird der Video-Decoder nachgezogen."""
        if enabled is not None:
            self.video_enabled = enabled
        else:
            self.video_enabled = not self.video_enabled
        if self.video_enabled and self._source is not None and self._video_iter is None:
            self._video_iter = (
                self._source.video_frames(self._pos) if self._shows_video(self._source) else None)
        if not self.video_enabled:
            self._video_iter = None
        return self.video_enabled

    @property
    def position(self) -> float:
        return self._pos

    def status(self) -> dict:
        return {
            "state": self.state,
            "track": self.current,
            "requester": self.requester,
            "position": self._pos,
            "duration": self._source.duration if self._source else None,
            "volume": self.volume,
            "video": self.video_enabled,
            "queue": list(self.queue),
            "peers": self.hub.count,
        }

    # ------------------------------------------------------- Warteschlange
    def enqueue(self, key: str, requester: str = "") -> None:
        """Hinten anstellen – Wuensche von Menschen kommen aber vor die Autoplay-Titel."""
        if requester != AUTOPLAY:
            for i, (_, who) in enumerate(self.queue):
                if who == AUTOPLAY:
                    self.queue.insert(i, (key, requester))
                    return
        self.queue.append((key, requester))

    def clear_queue(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    # -------------------------------------------------------------- intern
    async def _on_track_end(self) -> None:
        log.debug("Titel zu Ende")
        finished = self.current
        await self._teardown(cut=False)   # Rest im Puffer ist das echte Songende
        if self.on_event:
            await self.on_event("track-end", {"track": finished})
        await self._advance(auto=True, prev=finished)

    async def _advance(self, *, auto: bool, prev: Track | None = None) -> bool:
        if auto and prev is not None and self.moderation_on:
            if not await self._moderate(prev):
                return False             # waehrend der Ansage gestoppt/uebersprungen/neu gestartet
        if not self.queue:
            self.state = "stopped"
            if self.on_event:
                await self.on_event("queue-empty", {"auto": auto})
            return False
        key, requester = self.queue.pop(0)
        track = self.store.get(key)
        if track is None or (track.url == "" and not track.playable):
            log.warning("Nächster Titel %s fehlt – übersprungen", key)
            if self.on_event:
                await self.on_event("missing", {"key": key})
            return await self._advance(auto=auto)
        try:
            await self.play(track, requester)
        except Exception:
            log.exception("Nächster Titel %s nicht abspielbar – übersprungen", key)
            if self.on_event:
                await self.on_event("missing", {"key": key})
            return await self._advance(auto=auto)
        if self.on_event:
            await self.on_event("advanced", {"track": track, "requester": requester})
        return True

    async def _teardown(self, *, cut: bool = True) -> None:
        await self._end_fade()
        if self.current is not None:
            self.store.release_file(self.current.key)
        if self._source is not None:
            await asyncio.to_thread(self._source.close)
        self._source = None
        self._audio_iter = self._video_iter = None
        self._pos = self._vpos = 0.0
        self._voice = None
        if cut:
            self.hub.fade_out(CUT_FADE)

    # ------------------------------------------------------------ Crossfade
    def _new_source(self, track: Track) -> MediaSource:
        return MediaSource(track.abs_path, video_height=self.cfg.video_height,
                           video_fps=self.cfg.video_fps)

    def _release(self, key: str) -> None:
        """Datei freigeben – aber nie die des laufenden Titels."""
        if self.current is None or self.current.key != key:
            self.store.release_file(key)

    def _fade_seconds(self, old: float | None, new: float | None) -> float:
        """Ueberblendung, bei kurzen Titeln hoechstens ein Drittel des Titels."""
        fade = self.cfg.crossfade_seconds
        for dur in (old, new):
            if dur:
                fade = min(fade, dur / 3)
        return max(0.0, fade)

    def _maybe_prefetch(self) -> None:
        """Rechtzeitig vor dem Ende den naechsten Titel laden und den Decoder oeffnen."""
        self._maybe_need_next()
        if (self.cfg.crossfade_seconds <= 0 and not self.moderation_on) or not self.queue \
                or self._source is None:
            return
        dur = self._source.duration
        if not dur or self._pos < dur - self.cfg.crossfade_seconds - PREFETCH_SECONDS:
            return
        key = self.queue[0][0]
        if (self.current is not None and key == self.current.key) or key == self._prefetch_key:
            return  # gleicher Titel nochmal (dieselbe Datei) bzw. schon geladen
        if self._prefetch_task is not None and not self._prefetch_task.done():
            return  # alter Ladevorgang laeuft noch
        self._discard_prepared()
        self._prefetch_key = key
        self._prefetch_task = asyncio.create_task(self._prefetch(key), name="prefetch")

    def _maybe_need_next(self) -> None:
        """Einmal pro Titel melden, wenn die Warteschlange kurz vor dem Ende leer ist."""
        if self.queue or self._source is None or self.current is None:
            return
        dur = self._source.duration
        lead = self.cfg.crossfade_seconds + PREFETCH_SECONDS + NEED_NEXT_SECONDS
        if not dur or self._pos < dur - lead or self._need_next_for == self.current.key:
            return
        self._need_next_for = self.current.key
        self._emit_bg(("need-next", {"track": self.current}))

    async def _prefetch(self, key: str) -> None:
        track = self.store.get(key)
        if track is None:
            return
        try:
            if self.fetch is not None:
                await self.fetch(track, quiet=True)
            source = self._new_source(track)
            await asyncio.to_thread(source.open)
        except Exception:  # noqa: BLE001 – wird beim normalen Start nochmal versucht
            log.warning("Vorladen von %s fehlgeschlagen", key, exc_info=True)
            return
        if self._prefetch_key != key:     # inzwischen ueberholt (Stop, Queue geaendert)
            await asyncio.to_thread(source.close)
            self._release(key)
            return
        self._prepared = _Prepared(key, track, source)
        log.debug("Vorgeladen: %s", key)

    async def _take_prepared(self, key: str) -> _Prepared | None:
        """Vorgeladenen Titel uebernehmen (laufendes Vorladen abwarten statt doppelt laden)."""
        task = self._prefetch_task
        if task is not None and not task.done() and self._prefetch_key == key:
            await asyncio.shield(task)
        prepared = self._prepared
        if prepared is None or prepared.key != key:
            return None
        self._prepared = None
        self._prefetch_key = None
        return prepared

    def _discard_prepared(self) -> None:
        prepared, self._prepared = self._prepared, None
        self._prefetch_key = None
        if prepared is not None:
            prepared.source.close()
            self._release(prepared.key)

    def _crossfade_due(self) -> bool:
        prepared = self._prepared
        if prepared is None or self._fade is not None or self._source is None:
            self._force_fade = self._force_fade and prepared is not None
            return False
        if not self.queue or self.queue[0][0] != prepared.key:
            self._discard_prepared()    # Warteschlange hat sich geaendert
            self._force_fade = False
            return False
        if self._force_fade:
            return True
        if self.moderation_on:
            return False                 # Titelwechsel mit Pause und Ansage statt Ueberblendung
        dur = self._source.duration
        fade = self._fade_seconds(dur, prepared.source.duration)
        return bool(dur) and fade > 0 and self._pos >= dur - fade

    def _start_crossfade(self) -> None:
        """Naechsten Titel starten; der alte laeuft weiter und wird ausgeblendet."""
        prepared, self._prepared, self._prefetch_key = self._prepared, None, None
        assert prepared is not None and self._source is not None and self.current is not None
        _, requester = self.queue.pop(0)
        old = self.current
        if self._force_fade:          # von Hand ausgeloest: mindestens 2 s, auch wenn Crossfade aus ist
            fade = min(max(self.cfg.crossfade_seconds, 2.0), (prepared.source.duration or 60) / 3)
            self._force_fade = False
        else:
            fade = self._fade_seconds(self._source.duration, prepared.source.duration)
        self._fade = _Fade(old, self._source, self._audio_iter,
                           total=max(1, int(fade * SAMPLE_RATE)))
        self._wall_base += self._pos          # neuer Titel beginnt genau hier
        self.current, self.requester, self._source = prepared.track, requester, prepared.source
        self._audio_iter = prepared.source.audio_blocks(0.0)
        self._video_iter = (prepared.source.video_frames(0.0)
                            if self._shows_video(prepared.source) else None)
        self._pos = self._vpos = 0.0
        self._voice = None
        self._push_cover()
        self._on_new_track()
        log.info("Crossfade (%.1fs): %s -> %s", fade, old.title or old.key,
                 prepared.track.title or prepared.track.key)
        # Ereignisse im Hintergrund: Karten/Netzwerk duerfen den Mix nicht aufhalten
        self._emit_bg(("track-end", {"track": old}),
                      ("advanced", {"track": prepared.track, "requester": requester}))

    async def _mix_fade(self, new: "np.ndarray") -> "np.ndarray":
        fade = self._fade
        assert fade is not None
        old = None
        if fade.audio_iter is not None:
            try:
                old = await _next_async(fade.audio_iter)
            except Exception:  # noqa: BLE001 – Ende oder Decoderfehler: alter Titel ist still
                fade.audio_iter = None
        n = new.shape[1] // 2
        t0 = fade.done / fade.total
        fade.done += n
        block = crossfade_mix(old, new, t0, min(1.0, fade.done / fade.total), self.gain)
        if fade.done >= fade.total:
            await self._end_fade()
        return block

    # ------------------------------------------------- Ein-/Ausblenden (Master)
    def _ramp(self, target: float, seconds: float) -> None:
        self._master_target = max(0.0, min(1.0, target))
        dist = abs(self._master_target - self._master)
        self._master_rate = dist / (seconds * SAMPLE_RATE) if seconds > 0 else 1.0

    def _apply_master(self, block: "np.ndarray") -> "np.ndarray":
        g0, target = self._master, self._master_target
        if g0 == target:
            return block if g0 == 1.0 else fade_block(block, g0, g0)
        step = self._master_rate * (block.shape[1] // 2)
        g1 = min(target, g0 + step) if target > g0 else max(target, g0 - step)
        self._master = g1
        return fade_block(block, g0, g1)

    async def fade_out(self, seconds: float) -> bool:
        """Langsam ausblenden und warten, bis es leise ist. False: inzwischen wurde etwas
        anderes gestartet (dann nicht stoppen/ueberspringen)."""
        if self.state != "playing" or seconds <= 0:
            return True
        self._ramp(0.0, seconds)
        await asyncio.sleep(seconds + LEAD_SECONDS)
        return self._master_target == 0.0

    def reset_master(self) -> None:
        self._master = self._master_target = 1.0

    async def crossfade_now(self) -> bool:
        """Sofort zum naechsten Titel ueberblenden. False: der naechste Titel ist (noch) nicht
        geladen – dann bitte normal ueberspringen."""
        if not self.queue or self._source is None or self.state != "playing":
            return False
        key = self.queue[0][0]
        if self.current is not None and key == self.current.key:
            return False
        if self._prepared is None or self._prepared.key != key:
            if self._prefetch_key != key:
                self._discard_prepared()
                self._prefetch_key = key
                self._prefetch_task = asyncio.create_task(self._prefetch(key), name="prefetch")
            task = self._prefetch_task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), 4.0)
                except asyncio.TimeoutError:
                    return False
        if self._prepared is None or self._prepared.key != key:
            return False
        self.reset_master()
        self._force_fade = True
        return True

    async def _end_fade(self) -> None:
        fade, self._fade = self._fade, None
        if fade is not None:
            await asyncio.to_thread(fade.source.close)
            self._release(fade.track.key)

    # ------------------------------------------------------- Baer-DJ & Ansage
    def _baer_active(self) -> bool:
        if self.baer is None or not self.video_enabled or self.hub.count == 0:
            return False
        if self._speaking:                   # Moderationspause: keine Quelle, Baer redet
            return True
        return self._source is not None and not self._shows_video(self._source)

    def _shows_video(self, source) -> bool:
        """Eigenes Video des Titels zeigen? Nein, wenn der Baer darueber tanzen soll."""
        if source is None or not source.has_video or not self.video_enabled:
            return False
        return self.baer is None or not self.cfg.baer_over_video

    async def _baer_loop(self) -> None:
        """Baer-Bilder im Video-Takt in den Hub schieben (Rendern im Thread)."""
        loop = asyncio.get_running_loop()
        fps = max(1.0, min(30.0, float(self.cfg.video_fps)))
        anchor = loop.time()
        try:
            while True:
                baer = self.baer
                if baer is None:
                    return
                if not self._baer_active():
                    await asyncio.sleep(0.2)
                    anchor = loop.time()
                    continue
                try:
                    frame = await asyncio.to_thread(baer.render, loop.time())
                except Exception:  # noqa: BLE001
                    log.exception("Baer-Bild fehlgeschlagen – zurueck zum Titelbild")
                    self.baer = None
                    self._push_cover()
                    return
                if self._baer_active():
                    self.hub.push_video(frame)
                anchor += 1.0 / fps
                delay = anchor - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:
                    anchor = loop.time()
        except asyncio.CancelledError:
            raise

    @property
    def moderation_on(self) -> bool:
        return bool(self.cfg.moderation_enabled and self.ansager is not None and self.ansager.available)

    @staticmethod
    def _info(track: Track) -> tuple[str, str]:
        return spoken_title(track.title or track.key, track.uploader)

    async def _moderate(self, prev: Track) -> bool:
        """Pause zwischen zwei Titeln: Musik ist aus, der Baer redet. False = abgebrochen."""
        gen = self._gen
        self._speaking = True
        try:
            await self._moderate_inner(prev)
        finally:
            self._speaking = False
        return gen == self._gen

    async def _moderate_inner(self, prev: Track) -> None:
        song = self.baer.beat.song() if self.baer is not None else None   # wie war der letzte?
        if self.queue:
            key, requester = self.queue[0]
            nxt = self.store.get(key)
            if nxt is None:
                return
            info = self._info(nxt)
            if self.baer is not None:
                self.baer.set_track(*info)
            await self._speak(self.moderator.between(self._info(prev), info, requester, song),
                              "Jetzt kommt:", *info)
            self._no_intro_key = key
        else:
            await self._speak(self.moderator.end(self._info(prev), song),
                              "Danke fürs Zuhören!", f"{self.cfg.dj_name} macht Pause", "")

    async def _speak(self, text: str, headline: str, title: str, artist: str) -> None:
        """Text sprechen, ohne Musik (direkt in den Hub, im Echtzeit-Takt)."""
        if self.ansager is None:
            return
        gen = self._gen
        voice = await asyncio.to_thread(self.ansager.synth, text)
        if voice is None or gen != self._gen:
            return
        log.info("Moderation: %s", text)
        loop = asyncio.get_running_loop()
        t0 = loop.time() + self.hub.buffered_audio_seconds()   # erst nach dem Songende
        if self.baer is not None:
            self.baer.announce(t0, voice, headline=headline, title=title, artist=artist)
        gain = min(self.gain, 1.5)
        step = 960 * 2
        for i in range(0, voice.shape[1], step):
            if gen != self._gen:
                return
            # im Echtzeit-Takt schieben (wie der Mix-Loop), egal ob jemand zuhoert
            delay = t0 + i / 2 / SAMPLE_RATE - LEAD_SECONDS - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            block = voice[:, i:i + step]
            if block.shape[1] < step:
                block = np.pad(block, ((0, 0), (0, step - block.shape[1])))
            self.hub.push_audio(apply_gain(block, gain))
        end = t0 + voice.shape[1] / 2 / SAMPLE_RATE
        while loop.time() < end and gen == self._gen:
            await asyncio.sleep(0.05)

    def _on_new_track(self) -> None:
        """Neuer Titel laeuft an: Baer bekommt Titel/Titelbild, Ansage wird erzeugt."""
        track = self.current
        self._voice = None
        if track is None:
            return
        title, artist = self._info(track)
        if self.baer is not None:
            self.baer.set_track(title, artist, self.cover_frame)
            self.baer.beat.new_song(self._wall_base + self._pos)
        first = time.monotonic() - self._last_start > FRESH_START
        self._last_start = time.monotonic()
        if self._no_intro_key == track.key:          # in der Pause davor schon angesagt
            self._no_intro_key = None
            return
        self._no_intro_key = None
        if self.ansager is not None and self.ansager.available:
            text = (self.moderator.start((title, artist), first) if self.cfg.moderation_enabled
                    else text_for(title, artist))
            task = asyncio.create_task(self._prepare_ansage(track.key, text),
                                       name="ansage")
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        elif self.baer is not None:          # ohne Stimme: nur die Sprechblase
            self.baer.announce(self._wall_base + self._pos, None)

    async def _prepare_ansage(self, key: str, text: str) -> None:
        voice = await asyncio.to_thread(self.ansager.synth, text) if self.ansager else None
        if self.current is None or self.current.key != key or self._pos > ANSAGE_LATEST:
            return                           # inzwischen gewechselt/gespult: Ansage verfallen
        if voice is not None:
            self._voice, self._voice_i = voice, 0
            log.info("Ansage: %s", text)
        if self.baer is not None:
            self.baer.announce(self._wall_base + self._pos, voice)

    def _mix_voice(self, block: "np.ndarray") -> "np.ndarray":
        """Ansage ueber die Musik legen; die Musik wird dafuer weich leiser (DUCK)."""
        voice, i = self._voice, self._voice_i
        assert voice is not None
        n, total = block.shape[1], voice.shape[1]

        def duck(pos: int) -> float:
            t = pos / 2 / SAMPLE_RATE
            rest = max(0, total - pos) / 2 / SAMPLE_RATE
            return 1 - (1 - DUCK) * min(1.0, t / 0.25, rest / 0.4)

        out = fade_block(block, duck(i), duck(i + n)).astype(np.int32)
        seg = voice[:, i:i + n]
        out[:, : seg.shape[1]] += (seg.astype(np.float32) * min(self.gain, 1.5)).astype(np.int32)
        self._voice_i = i + n
        if self._voice_i >= total:
            self._voice = None
        return np.clip(out, -32768, 32767).astype(np.int16)

    def _emit_bg(self, *events: tuple[str, dict]) -> None:
        if self.on_event is None:
            return
        handler = self.on_event

        async def run() -> None:
            for name, data in events:
                try:
                    await handler(name, data)
                except Exception:  # noqa: BLE001
                    log.exception("Ereignis %s fehlgeschlagen", name)

        task = asyncio.create_task(run(), name="player-events")
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def shutdown(self) -> None:
        if self._mix_task is not None:
            self._mix_task.cancel()
            await asyncio.gather(self._mix_task, return_exceptions=True)
            self._mix_task = None
        for task in (self._baer_task, self._zuruf_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._baer_task = self._zuruf_task = None
        self._discard_prepared()
        await self._teardown()
        self.store.release_all()
        self.current = None
        self.state = "stopped"


_END = object()


def _next(iterator):
    """next() fuer asyncio.to_thread. StopIteration darf nicht durch ein Future laufen
    (RuntimeError), daher kommt am Ende _END zurueck."""
    if iterator is None:
        return _END
    return next(iterator, _END)


class _Exhausted(Exception):
    """Dekoder hat keine Daten mehr (StopIteration geht in Coroutinen nicht, PEP 479)."""


async def _next_async(iterator):
    item = await asyncio.to_thread(_next, iterator)
    if item is _END:
        raise _Exhausted
    return item
