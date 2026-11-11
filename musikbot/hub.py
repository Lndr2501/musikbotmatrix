"""Verteilt die gepufferten Frames an alle Teilnehmer eines Calls.

Der Player dekodiert und mischt genau einmal; jeder WebRTC-Peer bekommt einen
eigenen Puffer (Sink). Ist ein Puffer zu voll, verwirft er die ältesten Bilder –
dadurch bleibt ein langsamer Peer hinten nicht hängen und bremst niemanden aus.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import av
import numpy as np

from .media import fade_block

MAX_AUDIO_BLOCKS = 75  # 75 * 20 ms = 1,5 s
MAX_VIDEO_FRAMES = 4


@dataclass
class Sink:
    """Puffer eines einzelnen Teilnehmers."""

    audio: deque = field(default_factory=deque, repr=False)
    video: deque = field(default_factory=deque, repr=False)
    dropped_audio: int = 0
    dropped_video: int = 0

    def push_audio(self, block: np.ndarray) -> None:
        self.audio.append(block)
        while len(self.audio) > MAX_AUDIO_BLOCKS:
            self.audio.popleft()
            self.dropped_audio += 1

    def take_audio(self) -> np.ndarray | None:
        return self.audio.popleft() if self.audio else None

    def push_video(self, frame: av.VideoFrame, *, last: bool = False) -> None:
        if last:
            self.video.append(frame)
        else:
            self.video.append(frame)
            while len(self.video) > MAX_VIDEO_FRAMES:
                self.video.popleft()
                self.dropped_video += 1

    def take_video(self) -> av.VideoFrame | None:
        return self.video.popleft() if self.video else None

    def buffered_audio_seconds(self) -> float:
        return len(self.audio) * 0.02

    def clear(self) -> None:
        self.audio.clear()
        self.video.clear()

    def fade_out(self, seconds: float) -> float:
        """Statt hart abzuschneiden (Knacken): die naechsten `seconds` im Puffer
        ausblenden, den Rest verwerfen. Gibt die Laenge des Rests in Sekunden zurueck."""
        keep = list(self.audio)[: max(0, int(round(seconds / 0.02)))]
        self.audio.clear()
        self.video.clear()
        total = sum(b.shape[1] // 2 for b in keep)
        done = 0
        for block in keep:
            n = block.shape[1] // 2
            self.audio.append(fade_block(block, 1 - done / total, 1 - (done + n) / total))
            done += n
        return total / 48000


class Hub:
    def __init__(self) -> None:
        self.sinks: list[Sink] = []

    def new_sink(self) -> Sink:
        sink = Sink()
        self.sinks.append(sink)
        return sink

    def drop_sink(self, sink: Sink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    @property
    def count(self) -> int:
        return len(self.sinks)

    def push_audio(self, block: np.ndarray) -> None:
        for sink in self.sinks:
            sink.push_audio(block)

    def push_video(self, frame: av.VideoFrame) -> None:
        for index, sink in enumerate(self.sinks):
            # Jeder Peer encodiert selbst, also braucht jeder ein eigenes Exemplar.
            sink.push_video(frame if index == 0 else frame.copy())

    def buffered_audio_seconds(self) -> float:
        if not self.sinks:
            return 0.0
        return min(sink.buffered_audio_seconds() for sink in self.sinks)

    def clear(self) -> None:
        for sink in self.sinks:
            sink.clear()

    def fade_out(self, seconds: float) -> float:
        """Alle Puffer kurz ausblenden (siehe Sink.fade_out); gibt den laengsten Rest zurueck."""
        return max((sink.fade_out(seconds) for sink in self.sinks), default=0.0)
