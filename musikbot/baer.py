"""Baer-DJ: animiertes Kamerabild fuer Titel ohne eigenes Video.

Ein Baer steht am DJ-Pult und tanzt im Takt. Die Bewegungen sind vorgerenderte Loops
(Blender, ``tools/baer3d.py``): jeder Loop ist genau 4 Beats lang. Eine Beat-Uhr laeuft
im Songtempo und wird weich an die erkannten Kicks angeglichen – der Loop sitzt damit auf
dem Takt, ohne zu springen. Gewechselt wird nur an Taktgrenzen (bei Drops und Ansagen am
naechsten Beat), mit kurzer Ueberblendung.

Takt, Bass und Drops kommen live aus dem Audio, das der Player ausgibt (`Beat.feed`).
Alles laeuft auf der CPU.

Bildpakete (``BAER_ASSETS``): Ordner mit ``manifest.json`` und je Clip einem Unterordner
mit Bildern (webp/png, RGBA). Clips mit nur einem Bild (``"static": true``) wippen per Code.
Optional ``desk/``: das DJ-Pult aus derselben Kamera, einmal gedimmt (``base``) und einmal
voll leuchtend (``glow``). Der Bot legt es unter den Baeren (die Clips sind dort, wo das Pult
vor ihm steht, schon ausgeschnitten) und blendet LED-Front, Kanten, Pegel und Jogwheels live
zwischen beiden Bildern.
"""

from __future__ import annotations

import io
import json
import logging
import math
import random
import threading
from collections import OrderedDict, deque
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

ASSETS = Path(__file__).with_name("baer_assets")
SAMPLE_RATE = 48000
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
HUES = ((255, 60, 200), (60, 220, 255), (255, 200, 40), (120, 255, 120))
# Plaetze fuers Publikum (Mitte x, Mitte y als Anteil des Bilds), abwechselnd links/rechts;
# frei von Baer, Sprechblase (oben links) und Titelbild (oben rechts)
AUDIENCE_SLOTS = ((0.13, 0.47), (0.87, 0.47), (0.25, 0.33), (0.75, 0.33),
                  (0.05, 0.31), (0.95, 0.31), (0.19, 0.62), (0.81, 0.62))
AUDIENCE_R = 0.08            # Radius (Anteil der Bildhoehe)
DESK_Y = 0.70             # Oberkante Pult (Anteil der Bildhoehe)
HEAD_Y = 0.11             # Kopf-Oberkante des Baeren
FLOOR_Y = 0.935           # Boden unter dem Pult (Pakete mit "anchors")
HEAD_Y_3D = 0.10          # Kopf-Oberkante bei Paketen mit "anchors"
XFADE = 0.22              # Ueberblendung bei spontanen Wechseln (Reden), sonst Ein-/Ausstieg (s)
MOUTH_OPEN = 0.3          # ab dieser Lautheit der Stimme ist der Mund offen
HALF_TIME_ABOVE = 135.0   # schnellere Songs: Baer tanzt halbes Tempo (sieht natuerlicher aus)
CACHE_FRAMES = 96
# FFT-Bins (1024 @ 48 kHz, ~47 Hz je Bin) -> 12 Baender von ~94 Hz bis ~7,7 kHz fuer die EQ-Wand
BAND_EDGES = (2, 3, 4, 6, 9, 13, 19, 27, 39, 56, 80, 115, 165)
BRIGHT_BIN = 56             # ab ~2,6 kHz zaehlt als "hell" (Hi-Hats, Becken)
CALM_ENERGY = 0.35          # darunter (geglaettete Lautheit) gilt ein Song als ruhig


def _font(size: int, bold: bool = True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default(size)


# ---------------------------------------------------------------- Audio-Merkmale
def _tempo(gaps: list[float]) -> float:
    """BPM aus Kick-Abstaenden. Die Abstaende sind auf 20-ms-Bloecke gerastert (0,40 s = 150,
    0,41 s = 146 BPM); der Mittelwert der Abstaende nahe am Median trifft das Tempo dazwischen."""
    med = float(np.median(gaps))
    near = [g for g in gaps if abs(g - med) <= 0.08 * med] or [med]   # zweigipflig: Median
    return 60.0 / (sum(near) / len(near))


class Beat:
    """Bass, Lautheit, Kicks und Tempo aus den ausgegebenen Audio-Bloecken.

    Jeder Block bekommt die Wanduhr-Zeit, zu der er zu hoeren ist (`at`). So passt das
    Bild zum Ton, obwohl der Player etwas vorauspuffert.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.features: deque[tuple[float, float, float]] = deque(maxlen=600)  # (at, bass, rms)
        self.kicks: deque[float] = deque(maxlen=24)
        self.drops: deque[float] = deque(maxlen=8)
        self._bass_avg = 0.0
        self._bass_peak = 1.0
        self._short = 0.0
        self._long = 0.0
        self._window = np.hanning(1024).astype(np.float32)
        self._band_peak = np.full(len(BAND_EDGES) - 1, 1e-3, np.float32)
        self.new_song(0.0)

    # Song-Gefuehl: weich gemittelte Werte, damit der Baer zum Teil des Songs passt
    def new_song(self, at: float) -> None:
        """Ab Wanduhr-Zeit `at` laeuft ein neuer Titel: Song-Werte neu sammeln."""
        with getattr(self, "_lock", threading.Lock()):
            self._song_at = at
            self._e_fast = self._e_slow = self._low = self._bright = 0.0
            self._song_n = 0
            self._song_energy = self._song_bass = self._song_bright = 0.0
            self._song_peak = 0.0
            self._song_drops = 0
            self._song_bpms: list[float] = []
            self._song_last = at

    def feed(self, block: np.ndarray, at: float) -> None:
        mono = block.reshape(-1, 2).astype(np.float32).mean(1) / 32768.0
        if mono.size == 0:
            return
        rms = float(np.sqrt(np.mean(mono * mono)))
        seg = np.zeros(1024, np.float32)
        seg[: min(1024, mono.size)] = mono[:1024]
        spec = np.abs(np.fft.rfft(seg * self._window))
        bass = float(spec[1:4].sum())                          # ~47..190 Hz
        self._bass_peak = max(bass, self._bass_peak * 0.9995)  # langsam vergessen
        level = bass / (self._bass_peak + 1e-9)
        bands = np.add.reduceat(spec[:BAND_EDGES[-1]], BAND_EDGES[:-1]).astype(np.float32)
        total = float(spec[1:BAND_EDGES[-1]].sum()) + 1e-9
        bright = float(spec[BRIGHT_BIN:BAND_EDGES[-1]].sum()) / total   # Anteil Hoehen (Hi-Hats)
        self._band_peak = np.maximum(bands, self._band_peak * 0.9993)
        bands = np.sqrt(np.clip(bands / (self._band_peak + 1e-9), 0.0, 1.0))
        with self._lock:
            self.features.append((at, level, rms, bands))
            last = self.kicks[-1] if self.kicks else -1e9
            if level > 0.35 and level > 1.45 * self._bass_avg and at - last > 0.28:
                self.kicks.append(at)
            self._bass_avg = 0.92 * self._bass_avg + 0.08 * level
            self._short = 0.98 * self._short + 0.02 * rms
            self._long = 0.9975 * self._long + 0.0025 * rms
            last_drop = self.drops[-1] if self.drops else -1e9
            settled = at - self._song_at > 8 or self._song_at == 0   # Songanfang ist kein Drop
            if (self._short > 1.45 * self._long and self._short > 0.06 and at - last_drop > 10
                    and settled):
                self.drops.append(at)
                if at >= self._song_at:
                    self._song_drops += 1
            if at >= self._song_at and rms > 0.01:
                self._vibe_feed(at, min(1.0, rms * 4.0), level, bright)

    def _vibe_feed(self, at: float, loud: float, low: float, bright: float) -> None:
        def ema(old: float, new: float, tau: float) -> float:
            return old + (new - old) * (1 - math.exp(-dt / tau))
        dt = max(0.005, min(0.1, at - self._song_last))
        self._song_last = at
        first = self._song_n == 0
        self._e_fast = loud if first else ema(self._e_fast, loud, 1.5)
        self._e_slow = loud if first else ema(self._e_slow, loud, 8.0)
        self._low = low if first else ema(self._low, low, 2.0)
        self._bright = bright if first else ema(self._bright, bright, 3.0)
        n = self._song_n = self._song_n + 1
        self._song_energy += (loud - self._song_energy) / n
        self._song_bass += (low - self._song_bass) / n
        self._song_bright += (bright - self._song_bright) / n
        self._song_peak = max(self._song_peak, self._e_slow)
        if n % 250 == 0 and len(self.kicks) >= 7:            # alle ~5 s: Tempo merken
            ks = list(self.kicks)
            gaps = [b - a for a, b in zip(ks, ks[1:]) if 0.28 <= b - a <= 0.9]
            if len(gaps) >= 6:
                self._song_bpms.append(_tempo(gaps[-12:]))

    def _section(self, now: float, drop_at: float | None) -> str:
        """drop | build | break | calm | groove – grob, aus Energie und Bass."""
        if drop_at is not None and now - drop_at < 6.0:
            return "drop"
        if self._song_n < 250:                                   # erste ~5 s: noch nichts sagen
            return "calm" if self._e_fast < CALM_ENERGY else "groove"
        seconds = self._song_n * 0.02
        if seconds > 20 and self._song_bass > 0.2 and self._low < 0.45 * self._song_bass:
            return "break"                                       # Bass weg, Musik laeuft weiter
        if self._e_fast > 1.12 * self._e_slow and self._low < 0.85 * self._song_bass:
            return "build"                                       # wird lauter, Bass haelt sich zurueck
        if self._e_slow < CALM_ENERGY:
            return "calm"
        return "groove"

    def song(self) -> dict:
        """Zusammenfassung des laufenden/letzten Titels fuer die Moderation."""
        with self._lock:
            bpms = sorted(self._song_bpms)
            return {
                "seconds": self._song_n * 0.02,
                "energy": self._song_energy,
                "bass": self._song_bass,
                "bright": self._song_bright,
                "drops": self._song_drops,
                "bpm": bpms[len(bpms) // 2] if bpms else 0.0,
            }

    def reset(self) -> None:
        with self._lock:
            self.features.clear()
            self.kicks.clear()
            self.drops.clear()
            self._bass_avg = self._short = self._long = 0.0
        self.new_song(0.0)

    def state(self, now: float) -> dict:
        """Was gerade zu hoeren ist: Bass, Lautheit, Kicks, gemessenes Tempo, Drop."""
        with self._lock:
            bass = rms = 0.0
            bands = None
            heard = False
            for at, b, r, bd in reversed(self.features):
                if at <= now:
                    if now - at < 0.5:          # sonst: Pause/Stille
                        bass, rms, heard, bands = b, r, True, bd
                    break
            kicks = [k for k in self.kicks if k <= now]
            drops = [d for d in self.drops if d <= now]
            section = self._section(now, drops[-1] if drops else None)
            energy, low, bright = self._e_fast, self._low, self._bright
        gaps = [b - a for a, b in zip(kicks, kicks[1:]) if 0.28 <= b - a <= 0.9]
        bpm = _tempo(gaps[-12:]) if len(gaps) >= 6 else 0.0
        return {
            "bass": min(1.0, bass),
            "loud": min(1.0, rms * 4.0),
            "audible": heard and rms > 0.01,
            "kicks": kicks,
            "bpm": bpm,
            "drop_at": drops[-1] if drops else None,
            "bands": bands if bands is not None else np.zeros(len(BAND_EDGES) - 1, np.float32),
            "energy": energy if heard else 0.0,      # weich gemittelte Lautheit (~1,5 s)
            "low": low,                              # Bass-Anteil (~2 s)
            "bright": bright,                        # Hoehen-Anteil (Hi-Hats, ~3 s)
            "section": section if heard else "calm",
        }


class BeatClock:
    """Laeuft gleichmaessig im Tanztempo; erkannte Kicks ziehen die Phase nur sanft nach.

    So springt die Animation nie – auch wenn die Kick-Erkennung mal daneben liegt."""

    def __init__(self) -> None:
        self.pos = 0.0           # Tanz-Beats seit Start
        self.bpm = 110.0         # Tanztempo
        self.song_bpm = 0.0      # gemessenes Songtempo (0 = unbekannt)
        self._t: float | None = None
        self._seen = 0.0

    def advance(self, now: float, st: dict, calm_bpm: float | None = None) -> float:
        dt = 0.0 if self._t is None else max(0.0, min(0.25, now - self._t))
        self._t = now
        measured = st["bpm"]
        if measured:
            self.song_bpm = measured
            target = measured / 2 if measured > HALF_TIME_ABOVE else measured
            rate = 0.3 if abs(target - self.bpm) > 0.15 * self.bpm else 0.05
            self.bpm += (target - self.bpm) * rate
        tempo = calm_bpm if calm_bpm else self.bpm
        self.pos += dt * tempo / 60.0
        if calm_bpm or not measured:
            self._seen = max([self._seen, *st["kicks"]])
            return self.pos
        grid = self.bpm / self.song_bpm if self.song_bpm else 1.0   # 1 oder 0.5 (halbes Tempo)
        for k in st["kicks"]:
            if k <= self._seen:
                continue
            self._seen = k
            at_k = self.pos - (now - k) * tempo / 60.0
            err = round(at_k / grid) * grid - at_k
            self.pos += 0.2 * err
        return self.pos


# ---------------------------------------------------------------- Bildpaket
class Pack:
    """Loops aus einem Ordner; Bilder bleiben komprimiert im RAM und werden bei Bedarf
    dekodiert + skaliert (kleiner LRU-Cache)."""

    def __init__(self, path: Path, width: int, height: int) -> None:
        path = Path(path)
        with open(path / "manifest.json") as fh:
            m = json.load(fh)
        self.fpb = int(m.get("fpb", 16))
        self.beats = int(m.get("beats", 4))
        self.static = bool(m.get("static", False))
        self.clips: dict[str, list[bytes]] = {}
        self.kind: dict[str, str] = {}
        self.talking: set[str] = set()
        self.edges: set[str] = set()     # Clips mit Ein-/Ausstieg ueber die Grundpose
        for name, info in m["clips"].items():
            variants = [name, name + "_open"] if info.get("talk") else [name]
            if info.get("edges"):
                variants += [name + "@in", name + "@out"]
            for variant in variants:
                d = path / variant
                files = sorted(d.glob("*.webp")) or sorted(d.glob("*.png"))
                if files:
                    self.clips[variant] = [f.read_bytes() for f in files]
            if name in self.clips:
                self.kind[name] = info.get("kind", "groove")
                if info.get("talk") and name + "_open" in self.clips:
                    self.talking.add(name)
                if name + "@in" in self.clips and name + "@out" in self.clips:
                    self.edges.add(name)
        if not self.clips:
            raise ValueError(f"Keine Clips in {path}")
        src_w, src_h = Image.open(io.BytesIO(next(iter(self.clips.values()))[0])).size
        if self.static:                  # einzelne Posen: wie gehabt, Fuesse knapp unter dem Pult
            self.scale = height * float(m.get("height", 0.78)) / float(m.get("cell", 512))
            self.top_y = None
        elif "anchors" in m:             # Bildzeilen von Boden und Kopf im Quellbild
            a = m["anchors"]
            self.scale = (FLOOR_Y - HEAD_Y_3D) * height / (a["floor"] - a["head"])
            self.top_y = int(HEAD_Y_3D * height - a["head"] * self.scale)
        else:                            # aeltere Pakete: Taille am Pult, Kopf oben
            u = m["units"]
            ppu = (DESK_Y - HEAD_Y) * height / (u["head"] - u["desk"])
            self.scale = (u["top"] - u["bottom"]) * ppu / src_h
            self.top_y = int(DESK_Y * height - (u["top"] - u["desk"]) * ppu)
        self.size = (max(1, int(src_w * self.scale)), max(1, int(src_h * self.scale)))
        self.stage = None
        if not self.static and m.get("stage"):
            try:
                self.stage = self._load_stage(path / "stage", m["stage"], width, height)
            except Exception:  # noqa: BLE001
                log.exception("Buehnenbild unbrauchbar – nehme den einfachen Hintergrund")
        self.desk = None
        if not self.static and m.get("desk"):
            try:
                self.desk = self._load_desk(path / "desk", m["desk"], width, height)
            except Exception:  # noqa: BLE001
                log.exception("Pult-Bilder unbrauchbar – zeichne das einfache Pult")
        self._cache: OrderedDict[tuple[str, int], Image.Image] = OrderedDict()
        self._lock = threading.Lock()

    def _load_stage(self, d: Path, info: dict, width: int, height: int) -> dict | None:
        """Buehnenbild aufs Ausgabebild legen; EQ-Wand und Scheinwerfer in Ausgabe-Pixeln."""
        f = next((d / f"bg.{ext}" for ext in ("webp", "jpg", "png") if (d / f"bg.{ext}").exists()), None)
        if f is None:
            return None
        pw, ph = info["size"]
        sw, sh = max(1, round(pw * self.scale)), max(1, round(ph * self.scale))
        x0 = (width - sw) // 2
        y0 = int(self.top_y - float(info.get("oy", 0)) * self.scale)
        canvas = Image.new("RGB", (width, height), (8, 6, 14))
        canvas.paste(Image.open(f).convert("RGB").resize((sw, sh), Image.LANCZOS), (x0, y0))
        eq = info.get("regions", {}).get("eq")
        if eq:
            eq = (int(x0 + eq[0] * self.scale), int(y0 + eq[1] * self.scale),
                  int(x0 + eq[2] * self.scale), int(y0 + eq[3] * self.scale))
        spots = [(x0 + a * self.scale, y0 + b * self.scale) for a, b in info.get("spots", [])]
        return {"bg": canvas, "eq": eq, "spots": [p for p in spots if -20 <= p[1] < height * 0.5]}

    def _load_desk(self, d: Path, info: dict, width: int, height: int) -> dict | None:
        """Pult-Bilder auf Ausgabegroesse bringen (gleiche Skala/Lage wie die Clips)."""
        def find(key):
            return next((d / f"{key}.{ext}" for ext in ("webp", "png") if (d / f"{key}.{ext}").exists()), None)
        files = {k: find(k) for k in ("base", "glow")}
        if None in files.values():
            return None
        pw, ph = info["size"]
        sw, sh = max(1, round(pw * self.scale)), max(1, round(ph * self.scale))
        x0 = (width - sw) // 2
        full = {}
        for key, f in files.items():
            img = Image.open(f).convert("RGBA").resize((sw, sh), Image.LANCZOS)
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            canvas.paste(img, (x0, self.top_y))
            full[key] = canvas
        box = full["base"].getbbox()
        if box is None:
            return None
        regions = {}
        for name, (a, b, c, e) in info.get("regions", {}).items():
            r = (max(0, int(x0 + a * self.scale)), max(0, int(self.top_y + b * self.scale)),
                 min(width, int(math.ceil(x0 + c * self.scale))),
                 min(height, int(math.ceil(self.top_y + e * self.scale))))
            if r[2] > r[0] and r[3] > r[1]:
                regions[name] = r
        return {"box": box, "base": full["base"].crop(box), "base_full": full["base"],
                "glow_full": full["glow"], "regions": regions}

    def names(self, kind: str) -> list[str]:
        return [n for n, k in self.kind.items() if k == kind]

    def frame(self, name: str, idx: int) -> Image.Image:
        frames = self.clips[name]
        idx %= len(frames)
        key = (name, idx)
        with self._lock:
            img = self._cache.get(key)
            if img is not None:
                self._cache.move_to_end(key)
                return img
        img = Image.open(io.BytesIO(frames[idx])).convert("RGBA")
        if self.static:
            img = img.resize((max(1, int(img.width * self.scale)), max(1, int(img.height * self.scale))),
                             Image.LANCZOS)
        elif img.size != self.size:
            img = img.resize(self.size, Image.BILINEAR)
        with self._lock:
            self._cache[key] = img
            while len(self._cache) > CACHE_FRAMES:
                self._cache.popitem(last=False)
        return img

    def edge_at(self, name: str, which: str, u: float) -> Image.Image:
        """Ein-/Ausstieg (`which` = "in"/"out"), u = 0..1 ueber den einen Beat."""
        frames = self.clips[f"{name}@{which}"]
        return self.frame(f"{name}@{which}", min(len(frames) - 1, max(0, int(u * len(frames)))))

    def frame_at(self, name: str, beats: float) -> Image.Image:
        """Bild eines Loops nach `beats` Beats seit Clip-Start."""
        n = len(self.clips[name])
        if n == 1:
            return self.frame(name, 0)
        return self.frame(name, int((beats % self.beats) * self.fpb * n / (self.fpb * self.beats)))


# ---------------------------------------------------------------- Regie
# Wie ein Move wirkt: (Energie 0..1, Tempo -1 langsam/0/+1 schnell, Fokus, passende Songteile).
# Fokus "bass" passt zu basslastigen Stellen, "bright" zu vielen Hi-Hats. Unbekannte Clips
# (eigene Pakete) bekommen einen Wert nach ihrer Sorte.
MOVES: dict[str, tuple[float, int, str, tuple[str, ...]]] = {
    "coffee": (0.05, -1, "", ("calm",)),
    "sway": (0.15, -1, "", ("calm", "break")),
    "heart": (0.2, -1, "", ("calm", "break")),
    "stretch": (0.25, -1, "", ("break",)),
    "bow": (0.3, -1, "", ("calm",)),
    "lookout": (0.35, 0, "", ("build",)),
    "wave": (0.35, 0, "", ("calm",)),
    "thumbsup": (0.4, 0, "", ()),
    "knobs": (0.4, 0, "", ("build",)),
    "cue": (0.45, 0, "", ("build",)),
    "mix": (0.45, 0, "", ()),
    "groove": (0.5, 0, "", ()),
    "lowpump": (0.5, -1, "bass", ("break",)),
    "scratch": (0.55, 1, "bright", ("build",)),
    "shimmy": (0.55, 1, "bright", ()),
    "pump": (0.6, 0, "bass", ()),
    "clap": (0.65, 0, "", ("build",)),
    "crowdwave": (0.7, -1, "", ("break", "drop")),
    "disco": (0.7, 0, "bright", ()),
    "robot": (0.7, 1, "bright", ()),
    "roof": (0.75, 0, "bass", ("drop",)),
    "fistpump": (0.75, 0, "bass", ("drop",)),
    "airdrum": (0.8, 1, "bright", ()),
    "dab": (0.8, 0, "", ("drop",)),
    "cheer": (0.85, 0, "", ("drop",)),
    "spin": (0.85, 0, "", ()),
    "headbang": (0.9, 1, "bass", ("drop",)),
    "jump": (0.95, 1, "bass", ("drop",)),
}
KIND_ENERGY = {"groove": 0.5, "hype": 0.8, "special": 0.3, "talk": 0.4, "shout": 0.6}
FAST_BPM, SLOW_BPM = 124.0, 96.0
BASSY, BRIGHT = 0.32, 0.22           # Schwellen fuer "low" und "bright" aus Beat.state


def move_fit(name: str, kind: str, st: dict) -> float:
    """Wie gut passt der Move zur Stelle im Song? (>0, groesser = besser)"""
    energy, tempo, focus, sections = MOVES.get(name, (KIND_ENERGY.get(kind, 0.5), 0, "", ()))
    w = math.exp(-((energy - st.get("energy", 0.5)) / 0.25) ** 2)
    bpm = st.get("bpm") or 0.0
    if bpm and tempo:
        fast, slow = bpm >= FAST_BPM, bpm < SLOW_BPM
        w *= 1.8 if (tempo > 0 and fast) or (tempo < 0 and slow) else 0.5 if (fast or slow) else 1.0
    if focus == "bass" and st.get("low", 0.0) >= BASSY:
        w *= 1.6
    if focus == "bright" and st.get("bright", 0.0) >= BRIGHT:
        w *= 1.6
    if st.get("section") in sections:
        w *= 3.0
    return w + 0.03


class Director:
    """Waehlt, welcher Loop laeuft – passend zum Song: ruhige Stellen ruhige Moves, Build-ups
    am Pult schrauben, Drops Hype, Breaks schunkeln; schnelle Songs schnelle Moves. Reden bei
    Ansagen, Rufen bei Zurufen, Kaffee in Stille. Wechsel an Taktgrenzen; die letzten Moves
    werden gemieden, damit es abwechslungsreich bleibt."""

    GROOVE_SWITCH = 0.6     # je Takt: anderer Groove-Move (ruhige Songs seltener, wilde oefter)
    HYPE_ON_LOUD = 0.4      # je Takt bei lauter Musik: Hype-Move
    SPECIAL_EVERY = 4       # fruehestens alle so viele Takte ein Special
    RECENT = 4              # so viele zuletzt gespielte Moves meiden

    def __init__(self, pack: Pack, rng: random.Random) -> None:
        self.pack = pack
        self.rng = rng
        self._recent: list[str] = []
        self.clip = self._pick("groove") or next(iter(pack.kind))
        self.start = 0.0
        self.prev: tuple[str, float] | None = None
        self.changed_at = -1e9
        self._bar = -1
        self._last_drop = None
        self._specials_due = 0
        self._section = "groove"
        self.pending: tuple[str, float] | None = None    # (Clip, ab Beat-Position) – geplanter Wechsel
        self.planned = False                             # laufender Clip kam ueber seinen Einstieg
        self._decided: tuple[float, int] | None = None
        self._recent.append(self.clip)

    def _pick(self, kinds: str | tuple[str, ...], avoid: str | None = None,
              st: dict | None = None) -> str | None:
        kinds = (kinds,) if isinstance(kinds, str) else kinds
        names = [n for k in kinds for n in self.pack.names(k)]
        if not names:
            return None
        fresh = [n for n in names if n != avoid and n not in self._recent]
        names = fresh or [n for n in names if n != avoid] or names
        if st is None:
            weights = [2.0 if n == "mix" else 1.0 for n in names]
        else:
            weights = [move_fit(n, self.pack.kind[n], st) for n in names]
        return self.rng.choices(names, weights)[0]

    def _switch(self, name: str | None, pos: float, now: float, at_beat: bool = False,
                planned: bool = False, cut: bool = False) -> None:
        self.pending = None
        if name is None or name == self.clip:
            return
        smooth = planned and self.clip in self.pack.edges and name in self.pack.edges
        # cut: harter Schnitt (beim Drop verdecken Blitz und Zoom ihn), sonst kurz ueberblenden
        self.prev = None if smooth or cut else (self.clip, self.start)
        self.clip = name
        self._recent = (self._recent + [name])[-self.RECENT:]
        self.start = pos if planned else math.floor(pos) if at_beat else math.floor(pos / 4) * 4
        self.planned = smooth
        self.changed_at = now

    def update(self, pos: float, now: float, st: dict, talking: bool, talk_kind: str = "talk") -> None:
        bar = int(pos // 4)
        hype_kind = "hype" if self.pack.names("hype") else "groove"
        if talking:                                # Zuruf: "shout"-Clips, sonst Reden
            for talk_kind in (talk_kind, "talk", "groove"):
                if self.pack.names(talk_kind):
                    break
            if self.pack.kind.get(self.clip) != talk_kind:
                self._switch(self._pick(talk_kind, self.clip), pos, now, at_beat=True)
            elif bar != self._bar and self.rng.random() < 0.5:
                self._switch(self._pick(talk_kind, self.clip), pos, now)
            self._bar = bar
            return
        self._bar = bar
        drop = st["drop_at"] is not None and now - st["drop_at"] < 8 * 60 / max(60.0, st["bpm"] or 120)
        song = dict(st, section="drop") if drop else st
        if drop and st["drop_at"] != self._last_drop:          # Drop: sofort (naechster Beat) jubeln
            self._last_drop = st["drop_at"]
            self._switch(self._pick(hype_kind, self.clip, song), pos, now, at_beat=True, cut=True)
            return
        if self.pending is not None and pos >= self.pending[1]:   # geplanter Wechsel ist dran
            name, at = self.pending
            self._switch(name, at, now, planned=True)
        # Entscheidung im letzten Beat jedes 4er-Loops des laufenden Clips: so kann der alte Clip
        # ueber seinen Ausstieg in die Grundpose laufen und der neue von dort einsteigen.
        rel = pos - self.start
        cyc = int(rel // 4)
        if rel - cyc * 4 < 3 or self._decided == (self.start, cyc) or self.pending is not None:
            return
        self._decided = (self.start, cyc)
        choice = self._decide(st, song, drop, hype_kind)
        if choice is not None and choice != self.clip:
            self.pending = (choice, self.start + 4 * (cyc + 1))

    def _decide(self, st: dict, song: dict, drop: bool, hype_kind: str) -> str | None:
        """Naechster Move (oder None = weiter so), passend zur Stelle im Song."""
        kind = self.pack.kind.get(self.clip)
        section = song.get("section", "groove")
        energy = song.get("energy", song["loud"])
        changed, self._section = section != self._section, section
        if not st["audible"]:
            return "coffee" if "coffee" in self.pack.kind else self._pick("groove")
        if drop:
            return self._pick(hype_kind, self.clip, song)
        if section == "build":                    # Spannung: am Pult schrauben, jeden Takt was anderes
            return self._pick(("groove", "special", "hype"), self.clip, song)
        if section in ("break", "calm"):         # ruhig: weniger wechseln, nichts Wildes
            if changed or kind == "hype" or self.rng.random() < 0.4:
                return self._pick(("groove", "special"), self.clip, song)
            return None
        if energy > 0.55 and self.rng.random() < self.HYPE_ON_LOUD * (0.5 + energy):
            return self._pick(hype_kind, self.clip, song)
        self._specials_due += 1
        if (self._specials_due >= self.SPECIAL_EVERY and self.pack.names("special")
                and self.rng.random() < 0.5):
            self._specials_due = 0
            return self._pick("special", self.clip, song)
        if kind != "groove" or self.rng.random() < self.GROOVE_SWITCH * (0.6 + 0.6 * energy):
            return self._pick("groove", self.clip, song)
        return None

# ---------------------------------------------------------------- Baer
class BaerDJ:
    def __init__(self, width: int, height: int, assets: str | Path | None = None) -> None:
        self.w, self.h = width, height
        self.assets = Path(assets) if assets else ASSETS
        self.beat = Beat()
        self.clock = BeatClock()
        self._ready = False
        self._lock = threading.Lock()
        self._particles: list[list] = []
        self._bpm_hist: deque[tuple[float, float]] = deque()   # (now, gemessen) fuer die Anzeige
        self._bpm_shown = 0
        self._bpm_changed = -1e9
        self._rng = random.Random(1)
        self.title = ""
        self.artist = ""
        self._cover = None
        self._talk_from = 0.0
        self._talk_until = 0.0
        self._talk_kind = "talk"
        self._bubble: tuple[str, str, str] = ("", "", "")
        self._voice_env: np.ndarray | None = None   # Lautheit der Ansage, 50 Werte/s
        self._bass = 0.0
        self._loud = 0.0
        self._meters = [0.0] * 8
        self._audience: list[tuple[str, Image.Image]] = []
        self._t = None

    # ------------------------------------------------------------ Steuerung
    def set_track(self, title: str, artist: str, cover: "av.VideoFrame | None" = None) -> None:
        self.title, self.artist = title, artist
        self._bpm_hist.clear()
        self._bpm_shown = 0
        self._cover = None
        if cover is not None:
            try:
                img = cover.to_image()
                bbox = img.convert("L").point(lambda v: 255 if v > 8 else 0).getbbox()
                img = img.crop(bbox) if bbox else img
                side = int(self.h * 0.24)
                img.thumbnail((side, side))
                self._cover = img.convert("RGB")
            except Exception:  # noqa: BLE001
                log.debug("Titelbild fuer Baer unbrauchbar", exc_info=True)

    def set_audience(self, faces: list) -> None:
        """Call-Teilnehmer [(Name, runder Avatar|None)] -> fertige Kreise fuers Bild."""
        d = max(8, int(self.h * AUDIENCE_R * 2))
        ready = []
        for i, (name, img) in enumerate(faces[:len(AUDIENCE_SLOTS)]):
            if img is None:
                img = _initials(name, d, HUES[i % len(HUES)])
            else:
                img = img.convert("RGBA").resize((d, d), Image.LANCZOS)
            ready.append((name, img))
        with self._lock:
            self._audience = ready

    def announce(self, start: float, voice: np.ndarray | None, *, headline: str = "Jetzt kommt:",
                 title: str | None = None, artist: str | None = None, seconds: float = 4.0,
                 kind: str = "talk") -> None:
        """Baer redet ab Wanduhr-Zeit `start` (Mund nach der Stimme), Sprechblase dazu.
        `kind="shout"` waehlt die Ruf-Moves (Zurufe mitten im Song)."""
        self._talk_kind = kind
        if voice is not None and voice.size:
            mono = np.abs(voice.reshape(-1, 2).astype(np.float32).mean(1))
            step = SAMPLE_RATE // 50
            env = np.array([mono[i:i + step].mean() for i in range(0, mono.size, step)])
            self._voice_env = env / (env.max() + 1e-9)
            seconds = mono.size / SAMPLE_RATE
        else:
            self._voice_env = None
        self._bubble = (headline, self.title if title is None else title,
                        self.artist if artist is None else artist)
        self._talk_from, self._talk_until = start, start + seconds

    def reset(self) -> None:
        self.beat.reset()
        self._talk_until = 0.0

    def talking(self, now: float) -> bool:
        return self._talk_from - 0.15 <= now < self._talk_until + 0.3

    # --------------------------------------------------------------- Assets
    def _load(self) -> None:
        from PIL import ImageEnhance
        W, H = self.w, self.h
        try:
            self.pack = Pack(self.assets, W, H)
        except Exception:  # noqa: BLE001
            if self.assets == ASSETS:
                raise
            log.exception("Baer-Paket %s unbrauchbar – nehme das eingebaute", self.assets)
            self.pack = Pack(ASSETS, W, H)
        log.info("Baer-Paket: %s (%d Clips)", self.assets, len(self.pack.kind))
        self.director = Director(self.pack, self._rng)
        stage = getattr(self.pack, "stage", None)
        if stage is not None:
            self._bg = [ImageEnhance.Brightness(stage["bg"]).enhance(0.8 + 0.05 * i) for i in range(8)]
        else:
            club = Image.open(ASSETS / "club.jpg").convert("RGB").resize((W, H), Image.LANCZOS)
            self._bg = [ImageEnhance.Brightness(club).enhance(0.55 + 0.08 * i) for i in range(8)]
        self._f_big = _font(max(12, H // 22))
        self._f_small = _font(max(9, H // 34))
        self._f_note = _font(max(12, H // 14), bold=False)
        self._ready = True

    # --------------------------------------------------------------- Bild
    def render(self, now: float) -> av.VideoFrame:
        with self._lock:
            if not self._ready:
                self._load()
            img = self._draw(now)
        return av.VideoFrame.from_ndarray(np.asarray(img), format="rgb24")

    def _voice(self, now: float) -> float:
        env = self._voice_env
        if env is None or not (self._talk_from <= now < self._talk_until):
            return 0.0
        return float(env[min(env.size - 1, int((now - self._talk_from) * 50))])

    def _bear(self, now: float, pos: float, talking: bool) -> Image.Image:
        pack, d = self.pack, self.director
        name = d.clip
        if talking and name in pack.talking and self._voice(now) > MOUTH_OPEN:
            name += "_open"
        rel = pos - d.start
        if d.pending is not None and d.clip in pack.edges and d.pending[0] in pack.edges \
                and d.pending[1] - 1 <= pos < d.pending[1]:
            img = pack.edge_at(d.clip, "out", pos - (d.pending[1] - 1))     # in die Grundpose
        elif d.planned and 0 <= rel < 1 and d.clip in pack.edges:
            img = pack.edge_at(d.clip, "in", rel)                            # aus der Grundpose
        else:
            img = pack.frame_at(name, rel)
        fade = (now - d.changed_at) / XFADE
        if d.prev is not None and 0 <= fade < 1:
            old = pack.frame_at(d.prev[0], pos - d.prev[1])
            if old.size == img.size:
                img = Image.blend(old, img, fade * fade * (3 - 2 * fade))
        return img

    def _draw(self, now: float):
        W, H = self.w, self.h
        dt = 0.04 if self._t is None else max(0.0, min(0.25, now - self._t))
        self._t = now
        st = self.beat.state(now)
        talking = self.talking(now)
        calm = 72.0 if (talking or not st["audible"]) else None
        pos = self.clock.advance(now, st, calm)
        self.director.update(pos, now, st, talking, self._talk_kind)
        # weich nachgefuehrt statt flackernd
        a = 1 - math.exp(-dt / 0.08)
        self._bass += (st["bass"] - self._bass) * a
        self._loud += (st["loud"] - self._loud) * (1 - math.exp(-dt / 0.3))
        bass, loud = self._bass, self._loud
        ph = pos % 1.0
        pulse = 0.5 + 0.5 * math.cos(2 * math.pi * ph)        # 1 auf dem Beat, weich
        k = int(pos)
        hue = HUES[(k // 4) % 4]

        img = self._bg[min(7, int(bass * 7.99))].copy()
        stage = getattr(self.pack, "stage", None)
        if stage is not None and stage["eq"]:
            self._draw_eq(img, stage["eq"], st["bands"])
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        spots = stage["spots"] if stage is not None and stage["spots"] else \
            [(W * (0.15 + 0.23 * i), 0.0) for i in range(4)]
        for i, (sx, sy) in enumerate(spots):
            col = HUES[(i + k // 4) % len(HUES)] if stage is not None else hue
            x = sx + math.sin(now * 0.7 + i * 1.3) * W * 0.12
            wid = W * (0.07 + 0.03 * pulse)
            gd.polygon([(sx - 3, sy), (sx + 3, sy), (x + wid, H), (x - wid, H)],
                       fill=col + (int(14 + 40 * pulse * loud),))
        img.paste(glow, (0, 0), glow)
        if self._audience:
            self._draw_audience(img, pos, loud, now)

        desk = getattr(self.pack, "desk", None)
        if desk is not None:                                 # 3D-Pult unter dem Baeren
            self._draw_desk(img, desk, pos, bass, loud, pulse)
        self._draw_particles(img, now, hue)                  # hinter dem Baeren, nie im Gesicht
        spr = self._bear(now, pos, talking)
        if self.pack.static:                                 # einzelne Posen: Wippen per Code
            amp = 0.25 + 0.75 * loud
            sy = 1 - 0.05 * pulse * amp + 0.02 * amp
            spr = spr.resize((max(1, int(spr.width * (2 - sy))), max(1, int(spr.height * sy))),
                             Image.BILINEAR)
            dy = -H * 0.03 * amp * math.sin(math.pi * ph) ** 2
            img.paste(spr, ((W - spr.width) // 2, int(H * 0.80 - spr.height + dy)), spr)
        else:
            img.paste(spr, ((W - spr.width) // 2, self.pack.top_y), spr)

        dr = ImageDraw.Draw(img, "RGBA")
        if desk is None:
            self._draw_simple_desk(dr, pos, bass, hue)

        d_at = st["drop_at"]                                 # Drop: kurzer Zoom-Stoss + Blitz
        if d_at is not None and 0 <= now - d_at < 1.0:
            age = now - d_at
            z = 1 + 0.06 * math.exp(-age * 4.5) * min(1.0, age / 0.05)
            if z > 1.003:
                cw, ch = W / z, H / z
                x0, y0 = (W - cw) / 2, (H - ch) * 0.45
                img = img.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((W, H), Image.BILINEAR)
                dr = ImageDraw.Draw(img, "RGBA")
            flash = int(80 * math.exp(-age * 12))
            if flash > 3:
                dr.rectangle([0, 0, W, H], fill=(255, 255, 255, flash))

        if self._cover is not None:                          # Titelbild klein oben rechts
            c = self._cover
            x, y = W - c.width - int(W * .03), int(H * .04)
            r = max(3, H // 90)
            dr.rounded_rectangle([x - 4, y - 2, x + c.width + 6, y + c.height + 6], radius=r, fill=(0, 0, 0, 110))
            dr.rounded_rectangle([x - 3, y - 3, x + c.width + 2, y + c.height + 2], radius=r,
                                 fill=(255, 255, 255))
            img.paste(c, (x, y))

        headline, b_title, b_artist = self._bubble
        if talking and (b_title or headline):
            self._draw_bubble(img, now, headline, b_title, b_artist, hue)

        # Bauchbinde: halbtransparent, Neon-Linie im Takt, LIVE-Chip
        dr = ImageDraw.Draw(img, "RGBA")
        y0 = int(H * .935)
        dr.rectangle([0, y0, W, H], fill=(6, 4, 14, 200))
        dr.rectangle([0, y0, W, y0 + max(1, H // 240)], fill=hue + (int(140 + 100 * pulse),))
        f = self._f_small
        chip = "LIVE"
        cw = dr.textlength(chip, font=f) + H * .058
        cy0, cy1 = y0 + H * .012, H - H * .012
        dr.rounded_rectangle([W * .015, cy0, W * .015 + cw, cy1], radius=(cy1 - cy0) / 2, fill=(225, 30, 70))
        dot = (cy1 - cy0) * .22
        dcx, dcy = W * .015 + H * .018, (cy0 + cy1) / 2
        dr.ellipse([dcx - dot, dcy - dot, dcx + dot, dcy + dot], fill=(255, 255, 255, int(150 + 105 * pulse)))
        dr.text((W * .015 + H * .036, (cy0 + cy1) / 2), chip, font=f, fill=(255, 255, 255), anchor="lm")
        bpm = self._shown_bpm(now, st)
        line = f"♪ {self.title}" + (f"  ·  {bpm} BPM" if bpm and st["audible"] else "")
        tx = W * .015 + cw + W * .015
        dr.text((tx, (cy0 + cy1) / 2), _fit(dr, line, f, W - tx - W * .02), font=f,
                fill=(235, 235, 245), anchor="lm")
        return img


    def _shown_bpm(self, now: float, st: dict) -> int:
        """Ruhige BPM-Anzeige: Median der letzten 12 s, erst nach 3 s Messung, springt nur bei
        echter Aenderung (>= 3 BPM) und hoechstens alle 4 s."""
        if st["audible"] and st["bpm"]:
            self._bpm_hist.append((now, st["bpm"]))
        while self._bpm_hist and now - self._bpm_hist[0][0] > 12.0:
            self._bpm_hist.popleft()
        if len(self._bpm_hist) < 2 or self._bpm_hist[-1][0] - self._bpm_hist[0][0] < 3.0:
            return self._bpm_shown
        vals = sorted(b for _, b in self._bpm_hist)
        med = vals[len(vals) // 2]
        near = [b for b in vals if abs(b - med) <= 0.04 * med]
        if len(near) < 0.6 * len(vals):                     # Intro/Wechsel: noch nicht einig
            return self._bpm_shown
        cand = round(sum(near) / len(near))
        if not self._bpm_shown or (abs(cand - self._bpm_shown) >= 3 and now - self._bpm_changed >= 4.0):
            self._bpm_shown, self._bpm_changed = cand, now
        return self._bpm_shown

    def _bubble_vis(self, now: float) -> float:
        """0..1: wie sehr die Sprechblase gerade steht (fuer Platz-Machen der Avatare)."""
        headline, title, _ = self._bubble
        if not (title or headline) or not self.talking(now):
            return 0.0
        k = max(0.0, min(1.0, (now - self._talk_from) / 0.25, (self._talk_until + 0.3 - now) / 0.3))
        return k * k * (3 - 2 * k)

    def _draw_particles(self, img: Image.Image, now: float, hue: tuple) -> None:
        """Noten/Herzen bei Hype-Moves: steigen links und rechts neben dem Baeren auf
        und treiben nach aussen; gezeichnet vor dem Baeren-Sprite, also dahinter."""
        W, H = img.size
        if self.pack.kind.get(self.director.clip) == "hype" and self._rng.random() < 0.35:
            side = self._rng.choice((-1, 1))
            self._particles.append([W / 2 + side * self._rng.uniform(W * .24, W * .34),
                                    H * self._rng.uniform(.30, .50),
                                    side * self._rng.uniform(15, 60), self._rng.uniform(-150, -80),
                                    self._rng.choice("♪♫♥★"), hue, now])
        dr = ImageDraw.Draw(img, "RGBA")
        alive = []
        for p in self._particles:
            age = now - p[6]
            if 0 <= age < 1.6:
                alive.append(p)
                px = p[0] + p[2] * age * H / 720
                py = p[1] + (p[3] * age + 40 * age * age) * H / 720
                dr.text((px, py), p[4], font=self._f_note, fill=p[5] + (int(255 * (1 - age / 1.6)),))
        self._particles = alive[-60:]

    def _draw_bubble(self, img, now: float, headline: str, title: str, artist: str, hue) -> None:
        """Sprechblase: dunkle Glas-Karte mit Neon-Rand, ploppt auf, Spitze zeigt auf den Baeren."""
        W, H = self.w, self.h
        age = now - self._talk_from
        left = self._talk_until + 0.3 - now
        pop = min(1.0, max(0.0, age / 0.22))
        s = 1 + 2.2 * (pop - 1) ** 3 + 1.2 * (pop - 1) ** 2 if pop < 1 else 1.0   # easeOutBack
        alpha = max(0.0, min(1.0, left / 0.25, age / 0.08))
        if s <= 0.05 or alpha <= 0.02:
            return
        bw, bh = int(W * .335), int(H * .23)
        tail = int(H * .07)
        layer = Image.new("RGBA", (bw + 8, bh + tail + 8), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer, "RGBA")
        r = max(6, H // 28)
        accent = hue if self._talk_kind == "shout" else (180, 80, 255)      # Zuruf: Songfarbe
        d.rounded_rectangle([5, 7, bw + 5, bh + 7], radius=r, fill=(0, 0, 0, 120))             # Schatten
        tip = (int(bw * .93), bh + tail)
        d.polygon([(int(bw * .72), bh - 2), (int(bw * .86), bh - 2), tip], fill=accent + (255,))
        d.polygon([(int(bw * .735), bh - 4), (int(bw * .845), bh - 4), (tip[0] - 2, tip[1] - 5)],
                  fill=(18, 12, 34, 238))
        d.rounded_rectangle([0, 0, bw, bh], radius=r, fill=(18, 12, 34, 238), outline=accent + (255,),
                            width=max(2, H // 200))
        pad = int(W * .018)
        inner = bw - 2 * pad
        f_s, f_b = self._f_small, self._f_big
        tag = headline.rstrip(":").upper()
        tw = d.textlength(tag, font=f_s) + pad
        ty0, ty1 = int(bh * .09), int(bh * .09 + H * .045)
        d.rounded_rectangle([pad, ty0, pad + tw, ty1], radius=(ty1 - ty0) // 2, fill=accent + (255,))
        d.text((pad + pad / 2, (ty0 + ty1) / 2), tag, font=f_s, fill=(14, 8, 26), anchor="lm")
        line1, line2 = title, artist
        if not artist and d.textlength(title, font=f_b) > inner:     # Zuruf: umbrechen
            words, line1 = title.split(), ""
            while words and d.textlength((line1 + " " + words[0]).strip(), font=f_b) <= inner:
                line1 = (line1 + " " + words.pop(0)).strip()
            line1, line2 = (line1 or title), " ".join(words)
        y = ty1 + H * .015
        d.text((pad, y), _fit(d, line1, f_b, inner), font=f_b, fill=(255, 255, 255))
        if line2:
            y2 = y + H * .055
            if artist:
                d.text((pad, y2 + H * .004), _fit(d, line2, f_s, inner), font=f_s, fill=(200, 190, 230))
            else:
                d.text((pad, y2), _fit(d, line2, f_b, inner), font=f_b, fill=(255, 255, 255))
        if alpha < 1:
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
        ax, ay = int(W * .02), int(H * .05)
        if s < 0.999:                                   # aus der Spitze heraus aufploppen
            nw, nh = max(1, int(layer.width * s)), max(1, int(layer.height * s))
            ax, ay = ax + int(tip[0] * (1 - s)), ay + int(tip[1] * (1 - s))
            layer = layer.resize((nw, nh), Image.BILINEAR)
        img.paste(layer, (ax, ay), layer)

    def _draw_simple_desk(self, dr, pos: float, bass: float, hue) -> None:
        """Einfaches Pult (Pakete ohne eigene Pult-Bilder)."""
        W, H = self.w, self.h
        top = int(H * DESK_Y)
        dr.polygon([(W * .18, top), (W * .82, top), (W * .88, H), (W * .12, H)], fill=(22, 20, 34))
        dr.rectangle([W * .18, top - H * .03, W * .82, top], fill=(48, 44, 70))
        n = 16
        lit = bass * n
        for i in range(n):
            x0 = W * .22 + i * (W * .56 / n)
            on = max(0.0, min(1.0, lit - i))
            c = tuple(int(40 + (hc - 40) * on) for hc in hue)
            dr.rectangle([x0, top - H * .022, x0 + W * .56 / n * .7, top - H * .008], fill=c)
        for j, cx in enumerate((W * .32, W * .68)):           # Plattenteller drehen im Takt
            r, cy = H * .05, top + H * .05
            dr.ellipse([cx - r, cy - r * .4, cx + r, cy + r * .4], fill=(10, 10, 14))
            ang = pos * math.pi / 2 + j
            dr.line([cx, cy, cx + r * math.cos(ang), cy + r * .4 * math.sin(ang)],
                    fill=(230, 230, 240), width=max(1, H // 240))

    def _draw_eq(self, img, box, bands) -> None:
        """LED-Wand: Pegel je Frequenzband als Punktsaeulen, Bass innen am Baeren, Hoehen aussen."""
        W, H = self.w, self.h
        x0, y0, x1, y1 = box
        n = len(bands)
        if not hasattr(self, "_eq") or len(self._eq) != n:
            self._eq = np.zeros(n, np.float32)
        self._eq = np.maximum(bands, self._eq * 0.86)        # schnell hoch, weich runter
        cell = max(3, H // 70)
        rows = max(4, (y1 - y0) // cell)
        dr = ImageDraw.Draw(img, "RGBA")
        gap_l, gap_r = W * 0.36, W * 0.64                  # dort steht der Baer
        for side, (a, b) in enumerate(((x0, min(gap_l, x1)), (max(gap_r, x0), x1))):
            if b - a < cell * 2:
                continue
            cols = max(4, int((b - a) // (cell * 1.6)))
            for c in range(cols):
                f = c / max(1, cols - 1)
                band = int(round((1 - f if side == 0 else f) * (n - 1)))
                lit = int(self._eq[band] * rows)
                cx = a + (b - a) * (c + 0.5) / cols
                for r in range(lit):
                    t = r / rows
                    col = ((190, 70, 255) if side == 0 else (70, 140, 255))
                    col = tuple(int(v + (255 - v) * t * 0.5) for v in col)
                    y = y1 - (r + 1) * cell
                    dr.rectangle([cx - cell * 0.32, y + cell * 0.2, cx + cell * 0.32, y + cell * 0.8],
                                 fill=col + (int(150 + 90 * t),))

    def _draw_audience(self, img, pos: float, loud: float, now: float = 0.0) -> None:
        """Teilnehmer als leuchtende Kreise; huepfen versetzt im Takt. Wer unter der Sprechblase
        sitzt, tritt waehrenddessen zurueck (blendet fast aus) statt hervorzulugen."""
        W, H = self.w, self.h
        dr = ImageDraw.Draw(img, "RGBA")
        vis = self._bubble_vis(now)
        bub = (0, 0, W * .37, H * .34)                       # Sprechblase samt Spitze
        for i, (name, face) in enumerate(self._audience):
            sx, sy = AUDIENCE_SLOTS[i]
            hop = abs(math.sin(math.pi * (pos + i * 0.37)))   # jeder springt etwas anders
            d = face.width
            x = int(W * sx - d / 2)
            y = int(H * sy - d / 2 - H * 0.018 * (0.3 + 0.7 * loud) * hop)
            k = 1.0
            if vis > 0 and x < bub[2] and y < bub[3]:
                k = 1 - 0.85 * vis
            col = HUES[(i + int(pos) // 4) % len(HUES)]
            g = max(2, d // 14)
            dr.ellipse([x - 2 * g, y - 2 * g, x + d + 2 * g, y + d + 2 * g], fill=col + (int(40 * k),))
            dr.ellipse([x - g, y - g, x + d + g, y + d + g], fill=col + (int(200 * k),))
            if k < 1:
                face = face.copy()
                face.putalpha(face.getchannel("A").point(lambda a, k=k: int(a * k)))
            img.paste(face, (x, y), face)

    def _draw_desk(self, img, desk: dict, pos: float, bass: float, loud: float, pulse: float) -> None:
        """Pult aus Blender: gedimmtes Bild, darueber Leuchtflaechen live hochgeblendet."""
        base, glow_full, base_full, R = desk["base"], desk["glow_full"], desk["base_full"], desk["regions"]
        img.paste(base, desk["box"][:2], base)

        def glow(box, k):
            if box is None or k <= 0.03:
                return
            part = Image.blend(base_full.crop(box), glow_full.crop(box), min(1.0, k))
            img.paste(part, box[:2], part)

        glow(R.get("edges"), 0.3 + 0.7 * pulse * (0.35 + 0.65 * loud))
        glow(R.get("pads"), 0.35 + 0.65 * pulse)
        glow(R.get("front"), 0.12 + 0.88 * bass)
        for i in range(8):
            box = R.get(f"meter_{i}")
            if box is None:
                break
            target = min(1.0, bass * (0.75 + 0.1 * (i % 2)) + 0.35 * loud + self._rng.uniform(-0.08, 0.08))
            self._meters[i] += (target - self._meters[i]) * 0.5
            lit = max(0.0, self._meters[i])
            x0, y0, x1, y1 = box
            top = int(round(y1 - lit * (y1 - y0)))
            if top < y1:
                glow((x0, top, x1, y1), 1.0)
        dr = ImageDraw.Draw(img, "RGBA")
        for j, name in enumerate(("jog_L", "jog_R")):        # Jogwheels drehen im Takt
            box = R.get(name)
            if box is None:
                continue
            glow(box, 0.45 + 0.55 * loud)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            rx, ry = (box[2] - box[0]) / 2 * 0.62, (box[3] - box[1]) / 2 * 0.62
            ang = pos * math.pi / 2 * (1 if j == 0 else -1) + j * math.pi
            px, py = cx + rx * math.cos(ang), cy + ry * math.sin(ang)
            r = max(1.5, self.h / 200)
            dr.ellipse([px - r, py - r * 0.6, px + r, py + r * 0.6], fill=(235, 240, 255, 230))


def _initials(name: str, d: int, col) -> Image.Image:
    """Kreis mit Anfangsbuchstaben, wenn jemand kein Profilbild hat."""
    img = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.ellipse([0, 0, d - 1, d - 1], fill=tuple(int(c * 0.55) for c in col) + (255,))
    letter = (name.strip().lstrip("@")[:1] or "?").upper()
    font = _font(int(d * 0.5))
    w = dr.textlength(letter, font=font)
    dr.text(((d - w) / 2, d * 0.18), letter, font=font, fill=(255, 255, 255))
    return img


def _fit(draw, text: str, font, width: float) -> str:
    """Text kuerzen (mit …), bis er in `width` Pixel passt."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"
