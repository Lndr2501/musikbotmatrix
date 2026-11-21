"""Baer-DJ: animiertes Kamerabild fuer Titel ohne eigenes Video.

Ein Baer steht am DJ-Pult, wippt im Takt, jubelt bei Drops und sagt den naechsten
Titel an. Alles laeuft auf der CPU: Der Player fuettert jeden Audio-Block, den er
ausgibt (`feed`), daraus entstehen Bass, Lautheit, Kicks und Tempo. `render` baut
daraus mit Pillow ein Bild fuer den Zeitpunkt, der gerade zu hoeren ist.

Die Bilder (Posen + Club) wurden einmalig mit Qwen-Image erzeugt und liegen in
``baer_assets/``.
"""

from __future__ import annotations

import logging
import math
import random
import threading
from collections import deque
from pathlib import Path

import av
import numpy as np

log = logging.getLogger(__name__)

ASSETS = Path(__file__).with_name("baer_assets")
POSES = ("idle", "cheer", "talk", "jump")
CELL = 512                    # Groesse einer Zelle im Original-Posenblatt (gemeinsamer Massstab)
SAMPLE_RATE = 48000
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
HUES = ((255, 60, 200), (60, 220, 255), (255, 200, 40), (120, 255, 120))
TALK_TAIL = 0.4               # so lange bleibt die Sprechblase nach der Ansage stehen
BUBBLE_ONLY = 4.0             # Ansage ohne Stimme: Sprechblase so lange zeigen


def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default(size)


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
        with self._lock:
            self.features.append((at, level, rms))
            # Kick: Bass deutlich ueber seinem gleitenden Mittel, nicht zu dicht hintereinander
            last = self.kicks[-1] if self.kicks else -1e9
            if level > 0.35 and level > 1.45 * self._bass_avg and at - last > 0.28:
                self.kicks.append(at)
            self._bass_avg = 0.92 * self._bass_avg + 0.08 * level
            # Drop: Lautheit der letzten ~1 s springt ueber die der letzten ~8 s
            self._short = 0.98 * self._short + 0.02 * rms
            self._long = 0.9975 * self._long + 0.0025 * rms
            last_drop = self.drops[-1] if self.drops else -1e9
            if self._short > 1.45 * self._long and self._short > 0.06 and at - last_drop > 10:
                self.drops.append(at)

    def reset(self) -> None:
        with self._lock:
            self.features.clear()
            self.kicks.clear()
            self.drops.clear()
            self._bass_avg = self._short = self._long = 0.0

    def state(self, now: float) -> dict:
        """Was gerade zu hoeren ist: Bass, Lautheit, Beat-Phase, Tempo, Drop."""
        with self._lock:
            bass = rms = 0.0
            for at, b, r in reversed(self.features):
                if at <= now:
                    if now - at < 0.5:          # sonst: Pause/Stille
                        bass, rms = b, r
                    break
            kicks = [k for k in self.kicks if k <= now]
            drops = [d for d in self.drops if d <= now]
        gaps = [b - a for a, b in zip(kicks, kicks[1:]) if 0.28 <= b - a <= 0.9]
        period = float(np.median(gaps[-12:])) if len(gaps) >= 3 else 0.5
        anchor = kicks[-1] if kicks else 0.0
        beats = (now - anchor) / period
        return {
            "bass": min(1.0, bass),
            "loud": min(1.0, rms * 4.0),
            "phase": beats % 1.0,
            "beat": int(now / period),
            "bpm": 60.0 / period if len(gaps) >= 6 else 0.0,
            "drop": bool(drops) and now - drops[-1] < period * 8,
        }


class BaerDJ:
    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.beat = Beat()
        self._ready = False
        self._lock = threading.Lock()
        self._particles: list[list] = []
        self._rng = random.Random(1)
        self.title = ""
        self.artist = ""
        self._cover = None
        self._talk_from = 0.0
        self._talk_until = 0.0
        self._voice_env: np.ndarray | None = None   # Lautheit der Ansage, 50 Werte/s

    # ------------------------------------------------------------ Steuerung
    def set_track(self, title: str, artist: str, cover: "av.VideoFrame | None" = None) -> None:
        self.title, self.artist = title, artist
        self._cover = None
        if cover is not None:
            try:
                from PIL import Image
                img = cover.to_image() if hasattr(cover, "to_image") else Image.fromarray(
                    cover.to_ndarray(format="rgb24"))
                bbox = img.convert("L").point(lambda v: 255 if v > 8 else 0).getbbox()
                img = img.crop(bbox) if bbox else img
                side = int(self.h * 0.24)
                img.thumbnail((side, side))
                self._cover = img.convert("RGB")
            except Exception:  # noqa: BLE001
                log.debug("Titelbild fuer Baer unbrauchbar", exc_info=True)

    def announce(self, start: float, voice: np.ndarray | None) -> None:
        """Sprechblase (und Mundbewegung) ab Wanduhr-Zeit `start`."""
        if voice is not None and voice.size:
            mono = np.abs(voice.reshape(-1, 2).astype(np.float32).mean(1))
            step = SAMPLE_RATE // 50
            env = np.array([mono[i:i + step].mean() for i in range(0, mono.size, step)])
            self._voice_env = env / (env.max() + 1e-9)
            dur = mono.size / SAMPLE_RATE
        else:
            self._voice_env = None
            dur = BUBBLE_ONLY
        self._talk_from, self._talk_until = start, start + dur

    def reset(self) -> None:
        self.beat.reset()
        self._talk_until = 0.0

    # --------------------------------------------------------------- Assets
    def _load(self) -> None:
        from PIL import Image, ImageDraw, ImageEnhance
        W, H = self.w, self.h
        club = Image.open(ASSETS / "club.jpg").convert("RGB").resize((W, H), Image.LANCZOS)
        self._bg = [ImageEnhance.Brightness(club).enhance(0.55 + 0.08 * i) for i in range(8)]
        f = H * 0.78 / CELL
        self._pose = {}
        for p in POSES:
            im = Image.open(ASSETS / f"{p}.png").convert("RGBA")
            self._pose[p] = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))),
                                      Image.LANCZOS)
        desk = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        g = ImageDraw.Draw(desk)
        top = int(H * 0.70)
        g.polygon([(W * .18, top), (W * .82, top), (W * .88, H), (W * .12, H)], fill=(22, 20, 34, 255))
        g.rectangle([W * .18, top - H * .03, W * .82, top], fill=(48, 44, 70, 255))
        self._desk = desk
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

    def _draw(self, now: float):
        from PIL import Image, ImageDraw
        W, H = self.w, self.h
        s = self.beat.state(now)
        bass, loud, ph, k = s["bass"], s["loud"], s["phase"], s["beat"]
        kick = math.exp(-ph * 6)
        hue = HUES[(k // 4) % 4]

        img = self._bg[min(7, int(bass * 7.99))].copy()
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for i in range(4):
            x = W * (0.15 + 0.23 * i) + math.sin(now * 0.9 + i) * W * 0.05
            gd.polygon([(x, 0), (x - W * .12, H), (x + W * .12, H)],
                       fill=hue + (int(24 + 60 * kick * loud),))
        img.paste(glow, (0, 0), glow)

        talking = self._talk_from - 0.2 <= now < self._talk_until + TALK_TAIL
        if talking:
            pose = "talk"
        elif s["drop"]:
            pose = "jump" if k % 2 == 0 else "cheer"
        elif k % 16 in (12, 13) and loud > 0.45:
            pose = "cheer"
        else:
            pose = "idle"
        sp = self._pose[pose]
        amp = 0.25 + 0.75 * loud
        if talking:
            v = self._voice(now)
            sy, sx, dy, rot = 1 + 0.04 * v, 1 - 0.02 * v, -H * 0.01 * v, 0.0
        else:
            sy = 1 - 0.06 * kick * amp + 0.03 * (1 - kick) * amp
            sx = 1 + 0.05 * kick * amp
            jump = H * 0.10 * math.sin(math.pi * ph) if pose == "jump" else 0.0
            dy = -H * 0.035 * amp * math.sin(math.pi * ph) ** 2 - jump
            rot = 4 * math.sin(math.pi * (k + ph)) * amp
        spr = sp.resize((max(1, int(sp.width * sx)), max(1, int(sp.height * sy))), Image.BILINEAR)
        if rot:
            spr = spr.rotate(rot, resample=Image.BILINEAR, expand=True)
        img.paste(spr, ((W - spr.width) // 2, int(H * 0.80 - spr.height + dy)), spr)

        img.paste(self._desk, (0, 0), self._desk)
        dr = ImageDraw.Draw(img, "RGBA")
        top = int(H * 0.70)
        n = 16
        lit = int(bass * n + 0.5)
        for i in range(n):
            x0 = W * .22 + i * (W * .56 / n)
            dr.rectangle([x0, top - H * .022, x0 + W * .56 / n * .7, top - H * .008],
                         fill=hue if i < lit else (40, 40, 55))
        for cx in (W * .32, W * .68):                         # Plattenteller
            r, cy = H * .05, top + H * .05
            dr.ellipse([cx - r, cy - r * .4, cx + r, cy + r * .4], fill=(10, 10, 14))
            ang = now * 2 * math.pi * 0.55
            dr.line([cx, cy, cx + r * math.cos(ang), cy + r * .4 * math.sin(ang)],
                    fill=(230, 230, 240), width=max(1, H // 240))

        if pose in ("cheer", "jump") and self._rng.random() < 0.5:
            self._particles.append([W / 2 + self._rng.uniform(-W * .2, W * .2), H * .45,
                                    self._rng.uniform(-40, 40), self._rng.uniform(-160, -90),
                                    self._rng.choice("♪♫♥★"), hue, now])
        alive = []
        for p in self._particles:
            age = now - p[6]
            if 0 <= age < 1.6:
                alive.append(p)
                px = p[0] + p[2] * age * H / 720
                py = p[1] + (p[3] * age + 40 * age * age) * H / 720
                dr.text((px, py), p[4], font=self._f_note, fill=p[5] + (int(255 * (1 - age / 1.6)),))
        self._particles = alive[-60:]

        if self._cover is not None:                          # Titelbild klein oben rechts
            c = self._cover
            x, y = W - c.width - int(W * .03), int(H * .04)
            dr.rectangle([x - 3, y - 3, x + c.width + 2, y + c.height + 2], fill=(255, 255, 255))
            img.paste(c, (x, y))

        if talking and self.title:
            box = [W * .04, H * .05, W * .52, H * .26]
            dr.rounded_rectangle(box, radius=max(4, H // 30), fill=(255, 255, 255),
                                 outline=(20, 20, 30), width=max(1, H // 240))
            dr.polygon([(W * .36, H * .259), (W * .43, H * .259), (W * .44, H * .33)], fill=(255, 255, 255))
            pad = W * .02
            dr.text((box[0] + pad, box[1] + H * .025), "Jetzt kommt:", font=self._f_small, fill=(90, 90, 110))
            dr.text((box[0] + pad, box[1] + H * .07), _fit(dr, self.title, self._f_big, box[2] - box[0] - 2 * pad),
                    font=self._f_big, fill=(20, 20, 30))
            if self.artist:
                dr.text((box[0] + pad, box[1] + H * .135),
                        _fit(dr, self.artist, self._f_small, box[2] - box[0] - 2 * pad),
                        font=self._f_small, fill=(60, 60, 80))

        dr.rectangle([0, H * .94, W, H], fill=(0, 0, 0))
        line = f"♪ {self.title}" + (f"  ·  {s['bpm']:.0f} BPM" if s["bpm"] else "")
        dr.text((W * .02, H * .948), _fit(dr, line, self._f_small, W * .96), font=self._f_small,
                fill=(230, 230, 240))
        return img


def _fit(draw, text: str, font, width: float) -> str:
    """Text kuerzen (mit …), bis er in `width` Pixel passt."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"
