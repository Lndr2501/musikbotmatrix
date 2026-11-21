"""Ansage "Jetzt kommt: <Titel>" mit Piper (lokale Sprachausgabe auf der CPU).

Die Stimme wird beim ersten Gebrauch geladen (~0,3 s, ~150 MB RAM) und bleibt dann im
Speicher; eine Ansage zu erzeugen dauert ~0,1 s. Fehlt Piper oder die Stimme, bleibt
die Ansage einfach stumm – der Bot spielt normal weiter.
"""

from __future__ import annotations

import logging
import os
import re
import threading

import av
import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
LEAD_IN = 0.3      # Stille vor der Stimme (Musik wird in der Zeit leiser)
TAIL = 0.5         # Stille danach (Musik wird wieder lauter)

# "(Official Video)", "[Lyrics]", "| 4K" ... wirken vorgelesen albern
_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|video|lyric|audio|visuali[sz]er|hd|4k|remaster|clip|mv)[^\)\]]*[\)\]]",
    re.IGNORECASE)


def spoken_title(title: str, artist: str = "") -> tuple[str, str]:
    """Titel/Interpret aufraeumen; 'Interpret - Titel' wird aufgeteilt."""
    title = _NOISE.sub("", title or "").strip()
    title = re.split(r"\s+[|•]\s+", title)[0].strip()
    artist = (artist or "").removesuffix(" - Topic").strip()
    if " - " in title:
        left, right = title.split(" - ", 1)
        if not artist or artist.lower() in left.lower():
            artist, title = left.strip(), right.strip()
    return title or "der nächste Titel", artist


def text_for(title: str, artist: str = "") -> str:
    return f"Jetzt kommt: {title}" + (f" von {artist}" if artist else "") + "!"


class Ansager:
    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self._voice = None
        self._lock = threading.Lock()
        self.available = bool(model_path) and os.path.exists(model_path)
        if model_path and not self.available:
            log.warning("Ansage aus: Stimme %s fehlt", model_path)

    def _load(self):
        if self._voice is None:
            from piper import PiperVoice
            self._voice = PiperVoice.load(self.model_path)
            log.info("Ansage-Stimme geladen: %s", os.path.basename(self.model_path))
        return self._voice

    def synth(self, text: str) -> np.ndarray | None:
        """Sprachausgabe als int16-Bloecke wie der Player sie mischt: (1, n*2), 48 kHz stereo,
        mit kurzer Stille davor und danach. Blockiert – im Thread aufrufen."""
        if not self.available:
            return None
        try:
            with self._lock:
                voice = self._load()
                chunks = list(voice.synthesize(text))
        except Exception:  # noqa: BLE001
            log.warning("Ansage fehlgeschlagen, Ansage aus", exc_info=True)
            self.available = False
            return None
        if not chunks:
            return None
        mono = np.concatenate([c.audio_int16_array for c in chunks]).astype(np.int16)
        return to_stereo48k(mono, chunks[0].sample_rate)


def to_stereo48k(mono: np.ndarray, rate: int) -> np.ndarray:
    frame = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = rate
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
    parts = [f.to_ndarray().reshape(1, -1) for f in resampler.resample(frame)]
    parts += [f.to_ndarray().reshape(1, -1) for f in resampler.resample(None)]
    speech = np.concatenate(parts, axis=1).astype(np.int16)
    pad = lambda s: np.zeros((1, int(s * SAMPLE_RATE) * 2), np.int16)  # noqa: E731
    return np.concatenate([pad(LEAD_IN), speech, pad(TAIL)], axis=1)
