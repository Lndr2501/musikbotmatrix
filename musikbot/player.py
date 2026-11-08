"""Wiedergabe: Warteschlange, Mix-Loop, Pause/Resume/Seek/Lautstärke.

Der Mix-Loop ist die einzige Stelle, die dekodiert. Er hält die Audio-Position
an der Wanduhr, schiebt Blöcke in den Hub und treibt das Video in einem
eigenen, fps-begrenzten Takt hinterher.
"""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from typing import Awaitable, Callable, Optional

import numpy as np
from aiortc import MediaStreamTrack
from av import AudioFrame, VideoFrame

from .config import Config
from .hub import Hub, Sink
from .media import MediaSource, apply_gain, silence_block
from .store import Track, TrackStore

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
LEAD_SECONDS = 0.4  # Puffer, damit kleiner Jitter im Call nicht stört
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
        self._pos = 0.0
        self._vpos = 0.0
        self._wall_base = 0.0
        self._mix_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

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
                        block = await asyncio.to_thread(_next, self._audio_iter)
                    except StopIteration:
                        await self._on_track_end()
                        continue
                    except Exception:  # noqa: BLE001
                        log.exception("Dekoder abgestürzt")
                        await self._on_track_end()
                        continue
                    block = apply_gain(block, self.gain)
                    self.hub.push_audio(block)
                    self._pos += (block.shape[1] // 2) / SAMPLE_RATE
                    continue

                if self.video_enabled and self._video_iter is not None and self._vpos <= self._pos:
                    try:
                        when, frame = await asyncio.to_thread(_next, self._video_iter)
                    except StopIteration:
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
            await self._teardown()
            self.current = track
            self.requester = requester
            if self.fetch is not None:
                await self.fetch(track)  # immer frisch herunterladen
            source = MediaSource(
                track.abs_path,
                video_height=self.cfg.video_height,
                video_fps=self.cfg.video_fps,
            )
            await asyncio.to_thread(source.open)
            self._source = source
            self._start_streams(0.0)
            self.state = "playing"
            self._wall_base = asyncio.get_running_loop().time()
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
        self._wall_base = asyncio.get_running_loop().time() - start
        self.hub.clear()

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
            await self._teardown()
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
        await asyncio.to_thread(self._source.seek, seconds)
        self._start_streams(seconds)
        if self.state == "paused":
            self.state = "playing"
            self._wall_base = asyncio.get_running_loop().time() - seconds
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
        self.queue.append((key, requester))

    def clear_queue(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    # -------------------------------------------------------------- intern
    async def _on_track_end(self) -> None:
        log.debug("Titel zu Ende")
        finished = self.current
        await self._teardown()
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

    async def _teardown(self) -> None:
        if self.current is not None:
            self.store.release_file(self.current.key)
        if self._source is not None:
            await asyncio.to_thread(self._source.close)
        self._source = None
        self._audio_iter = self._video_iter = None
        self._pos = self._vpos = 0.0
        self.hub.clear()

    async def shutdown(self) -> None:
        if self._mix_task is not None:
            self._mix_task.cancel()
            await asyncio.gather(self._mix_task, return_exceptions=True)
            self._mix_task = None
        await self._teardown()
        self.store.release_all()
        self.current = None
        self.state = "stopped"


def _next(iterator):
    """next() als eigene Funktion, damit asyncio.to_thread damit klarkommt."""
    if iterator is None:
        raise StopIteration
    return next(iterator)
