"""Decodieren mit PyAV: gleichmässige Audio-Blöcke und videofähige Frames.

Der Player holt sich hier immer nur kleine Häppchen – dadurch funktionieren
Pause, Resume und Seek, ohne den ganzen Titel in den RAM zu laden.
Audio normalisieren wir auf 48 kHz stereo/s16, weil WebRTC/Opus genau das will.
"""

from __future__ import annotations

import logging
import threading
from fractions import Fraction
from typing import Iterator

import av

# PyAV hat die Fehlerklasse je nach Version unter anderem Namen (av.Error gibt es nicht mehr).
_AV_ERRORS = tuple(
    c for c in (getattr(av, "FFmpegError", None), getattr(getattr(av, "error", None), "FFmpegError", None),
                getattr(av, "AVError", None))
    if isinstance(c, type)
) or (Exception,)
import numpy as np

from .ytdl import MediaError

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms


class MediaSource:
    """Ein heruntergeladener Titel, bereit zum Abspielen."""

    def __init__(self, path: str, *, video_height: int = 480, video_fps: float = 24.0) -> None:
        self.path = path
        self.video_height = video_height
        self.video_fps = video_fps
        self.duration: float | None = None
        self.has_video = False
        self.video_size: tuple[int, int] | None = None
        self._audio_container: av.InputContainer | None = None
        self._video_container: av.InputContainer | None = None
        self._audio_stream = None
        self._video_stream = None
        # Dekodieren (Wiedergabe-Thread) und Schliessen/Spulen (anderer Thread) duerfen nie
        # gleichzeitig am selben Container arbeiten – sonst stuerzt PyAV/FFmpeg ab (Segfault).
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- open
    def open(self) -> None:
        try:
            probe = av.open(self.path)
        except av.Error as exc:
            raise MediaError(f"Kann Datei nicht lesen: {exc}") from exc
        try:
            self.duration = probe.duration / 1e6 if probe.duration else None
            vstream = next(iter(probe.streams.video), None)
            astream = next(iter(probe.streams.audio), None)
            self.has_video = vstream is not None
            if vstream is not None:
                src_h = vstream.height or self.video_height
                src_w = vstream.width or 1
                height = min(self.video_height, src_h)
                height -= height % 2
                width = int(src_w * height / max(1, src_h))
                width -= width % 2
                self.video_size = (max(16, width), max(16, height))
            if astream is None:
                raise MediaError("Die Datei enthält keinen Audiotrack.")
        finally:
            probe.close()
        self._reopen(0.0)

    def _reopen(self, start: float) -> None:
        with self._lock:
            self._reopen_locked(start)

    def _reopen_locked(self, start: float) -> None:
        self.close()
        self._audio_container = av.open(self.path)
        self._audio_stream = next(iter(self._audio_container.streams.audio))
        if self.has_video:
            self._video_container = av.open(self.path)
            self._video_stream = next(iter(self._video_container.streams.video))
        if start > 0:
            self.seek(start)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        for container in (self._audio_container, self._video_container):
            if container is not None:
                try:
                    container.close()
                except Exception:  # noqa: BLE001
                    log.debug("Container schon geschlossen", exc_info=True)
        self._audio_container = self._video_container = None
        self._audio_stream = self._video_stream = None

    # --------------------------------------------------------------- seek
    def seek(self, seconds: float) -> None:
        with self._lock:
            self._seek_locked(seconds)

    def _seek_locked(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        offset = int(seconds * 1_000_000)
        for container in (self._audio_container, self._video_container):
            if container is None:
                continue
            try:
                try:
                    container.seek(offset, backward=True, any_frame=False)
                except TypeError:  # aeltere PyAV-Versionen nannten den Parameter "any"
                    container.seek(offset, backward=True, any=False)
            except (_AV_ERRORS, ValueError):
                log.debug("Seek nicht möglich, Container wird neu geöffnet")
                self._reopen(seconds)
                return

    # -------------------------------------------------------------- audio
    def audio_blocks(self, start: float = 0.0) -> Iterator[np.ndarray]:
        """Liefert (1, n*2) int16-Blöcke à 20 ms, 48 kHz stereo, lückenlos."""
        with self._lock:
            if self._audio_container is None:
                self._reopen(start)
            assert self._audio_container is not None and self._audio_stream is not None
            if start > 0:
                self.seek(start)
            container = self._audio_container
            frames = container.decode(self._audio_stream)
        resampler = av.AudioResampler(
            format="s16", layout="stereo", rate=SAMPLE_RATE, frame_size=FRAME_SAMPLES)
        while True:
            with self._lock:
                if self._audio_container is not container:   # geschlossen oder neu geoeffnet
                    return
                frame = next(frames, None)
                if frame is None:
                    return
                outs = [out.to_ndarray() for out in resampler.resample(frame)]
            for arr in outs:
                if arr.dtype != np.int16:
                    arr = arr.astype(np.int16)
                if arr.ndim == 2 and arr.shape[0] == 1:
                    yield arr
                else:  # Sicherheitsnetz für exotische Layouts
                    yield np.ascontiguousarray(arr).reshape(1, -1)

    # --------------------------------------------------------------- video
    def video_frames(self, start: float = 0.0) -> Iterator[tuple[float, av.VideoFrame]]:
        """Liefert (zeit, frame) mit maximal video_fps Bildern pro Sekunde."""
        with self._lock:
            if not self.has_video or self._video_container is None or self._video_stream is None:
                return
            if start > 0:
                self.seek(start)
            container = self._video_container
            frames = container.decode(self._video_stream)
        step = 1.0 / max(1.0, self.video_fps)
        next_at = -1e9
        while True:
            with self._lock:
                if self._video_container is not container:   # geschlossen oder neu geoeffnet
                    return
                frame = next(frames, None)
                if frame is None:
                    return
                when = _frame_time(frame)
                if when + 1e-6 < next_at:
                    continue
                next_at = when + step
                if self.video_size:
                    width, height = self.video_size
                    if (frame.width, frame.height) != (width, height):
                        frame = frame.reformat(width=width, height=height,
                                               format="yuv420p")
            yield when, frame


def _frame_time(frame: av.VideoFrame) -> float:
    if frame.pts is None or frame.time_base is None:
        return 0.0
    base: Fraction = frame.time_base
    return float(frame.pts * base)


def apply_gain(block: np.ndarray, gain: float) -> np.ndarray:
    """Lautstärke ohne Übersteuern."""
    if gain == 1.0:
        return block
    return np.clip(block.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


def _progress(n: int, t0: float, t1: float) -> np.ndarray:
    """Fortschritt t0..t1 je Sample (stereo: jeder Wert doppelt fuer L/R)."""
    t = t0 + (t1 - t0) * (np.arange(n, dtype=np.float32) / max(1, n))
    return np.repeat(t, 2)


def fade_block(block: np.ndarray, g0: float, g1: float) -> np.ndarray:
    """Lineare Lautstaerke-Rampe von g0 nach g1 ueber einen Block."""
    n = block.shape[1] // 2
    ramp = _progress(n, g0, g1)
    out = block.astype(np.float32).reshape(-1)[: ramp.size] * ramp[: block.shape[1]]
    return np.clip(out, -32768, 32767).astype(np.int16).reshape(1, -1)


def crossfade_mix(old: np.ndarray | None, new: np.ndarray, t0: float, t1: float,
                  gain: float = 1.0) -> np.ndarray:
    """Equal-Power-Ueberblendung fuer einen Block: `old` wird leiser (cos), `new` lauter (sin).

    t0/t1 = Fortschritt der Ueberblendung (0..1) am Anfang/Ende dieses Blocks.
    `old` darf fehlen (Titel schon zu Ende) oder kuerzer/laenger sein als `new`.
    """
    t = _progress(new.shape[1] // 2, t0, t1)[: new.shape[1]]
    angle = np.clip(t, 0.0, 1.0) * (np.pi / 2)
    mixed = new.astype(np.float32).reshape(-1) * np.sin(angle)
    if old is not None:
        a = old.astype(np.float32).reshape(-1)
        a = np.pad(a, (0, mixed.size - a.size)) if a.size < mixed.size else a[: mixed.size]
        mixed += a * np.cos(angle)
    return np.clip(mixed * gain, -32768, 32767).astype(np.int16).reshape(1, -1)


def silence_block(samples: int = FRAME_SAMPLES) -> np.ndarray:
    return np.zeros((1, samples * 2), dtype=np.int16)


def cover_frame(data: bytes, width: int, height: int):
    """Titelbild -> Videobild width x height (zentriert, schwarze Raender, nichts wird gestreckt).
    Gibt None zurueck, wenn sich das Bild nicht lesen laesst."""
    import io

    import numpy as np
    try:
        with av.open(io.BytesIO(data)) as box:
            src = next(box.decode(video=0))
        scale = min(width / src.width, height / src.height)
        w = max(2, int(src.width * scale) // 2 * 2)
        h = max(2, int(src.height * scale) // 2 * 2)
        img = src.reformat(width=w, height=h, format="rgb24").to_ndarray()
        canvas = np.zeros((height, width, 3), dtype=np.uint8)
        y, x = (height - h) // 2, (width - w) // 2
        canvas[y:y + h, x:x + w] = img
        return av.VideoFrame.from_ndarray(canvas, format="rgb24")
    except (StopIteration, ValueError, *_AV_ERRORS):
        return None
