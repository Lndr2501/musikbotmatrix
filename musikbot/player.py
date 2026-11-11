"""Wiedergabe: Warteschlange, Mix-Loop, Pause/Resume/Seek/Lautstärke.

Der Mix-Loop ist die einzige Stelle, die dekodiert. Er hält die Audio-Position
an der Wanduhr, schiebt Blöcke in den Hub und treibt das Video in einem
eigenen, fps-begrenzten Takt hinterher.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from fractions import Fraction
from typing import Awaitable, Callable, Iterator, Optional

import numpy as np
from aiortc import MediaStreamTrack
from av import AudioFrame, VideoFrame

from .config import Config
from .hub import Hub, Sink
from .media import MediaSource, apply_gain, crossfade_mix, fade_block, silence_block
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
                    try:
                        block = await _next_async(self._audio_iter)
                    except _Exhausted:
                        await self._on_track_end()
                        continue
                    except Exception:  # noqa: BLE001
                        log.exception("Dekoder abgestürzt")
                        await self._on_track_end()
                        continue
                    if self._fade is not None:
                        block = await self._mix_fade(block)
                    else:
                        block = apply_gain(block, self.gain)
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
            if (self.video_enabled and self._source.has_video) else None
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
        self._push_cover()

    @property
    def cover_frame(self) -> VideoFrame | None:
        cur = self.current
        return self.cover[1] if self.cover and cur and self.cover[0] == cur.key else None

    def _push_cover(self) -> None:
        """Ohne eigenes Video bleibt die Kamera nicht schwarz: Titelbild (sonst ein schwarzes Bild)."""
        if not self.video_enabled or self._source is None or self._source.has_video:
            return
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
        async with self._lock:
            if clear_queue:
                self.queue.clear()
            self._discard_prepared()
            await self._teardown()
            self.reset_master()
            self.state = "stopped"

    async def skip(self) -> bool:
        """Aktuellen Titel beenden und nächsten starten."""
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
        self._start_streams(seconds)
        if self.state == "paused":
            self.state = "playing"
        return True

    def set_volume(self, value: int) -> int:
        self.volume = max(0, min(MAX_VOLUME, value))
        return self.volume

    def set_video(self, enabled: bool | None = None) -> bool:
        """Video an/aus; beim Einschalten wird der Video-Decoder nachgezogen."""
        if enabled is not None:
            self.video_enabled = enabled
        else:
            self.video_enabled = not self.video_enabled
        if self.video_enabled and self._source is not None and self._video_iter is None:
            self._video_iter = (
                self._source.video_frames(self._pos) if self._source.has_video else None)
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
        await self._advance(auto=True)

    async def _advance(self, *, auto: bool) -> bool:
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
        if self.cfg.crossfade_seconds <= 0 or not self.queue or self._source is None:
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
                            if (self.video_enabled and prepared.source.has_video) else None)
        self._pos = self._vpos = 0.0
        self._push_cover()
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
