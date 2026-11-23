"""Blick: der DJ schaut ab und zu auf eine Kamera oder einen geteilten Bildschirm im Call
und ruft einen kurzen Spruch dazu rein.

Ablauf pro Blick: Kandidat waehlen (geteilte Bildschirme zuerst, reihum, nur wer nicht
widersprochen hat) -> ein Standbild per kurzem Abo (`ElementCall.snapshot`) -> Bildmodell
(`KI.chat` mit Bild) -> `Player.shout`. Bilder werden nicht gespeichert; vom letzten Bild
pro Quelle bleibt nur ein 16x9-Graustufen-Fingerabdruck, damit ein unveraendertes Bild
nicht dauernd kommentiert wird.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, Optional

import numpy as np

from .ki import KI, clean_line, image_part
from .publikum import user_of

log = logging.getLogger("musikbot.blick")

FIRST_LOOK = (35.0, 70.0)      # erster Blick nach dem Beitritt (s)
RETRY_S = 12.0                 # Moment passt gerade nicht (Ansage, Songanfang): bald wieder
SAME_IMAGE = 7.0               # mittlere Grauwert-Differenz darunter = gleiches Bild
SAME_IMAGE_QUIET = 15 * 60.0   # gleiches Bild erst nach so langer Zeit wieder kommentieren
SKIP = "-"                     # Antwort des Modells: nichts Sinnvolles zu sehen

KIND_LABEL = {"camera": "Kamerabild", "screen": "geteilter Bildschirm"}


def fingerprint(img) -> np.ndarray:
    return np.asarray(img.convert("L").resize((16, 9)), dtype=np.float32)


def same_image(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> bool:
    return a is not None and b is not None and float(np.abs(a - b).mean()) < SAME_IMAGE


def system_prompt(dj: str) -> str:
    return (
        f"Du bist {dj}, ein frecher, herzlicher Plüschbär-DJ in einem Videocall unter Freunden. "
        "Du legst Musik auf und wirfst ab und zu einen kurzen Spruch über das rein, was du bei den Leuten siehst.\n"
        "Regeln:\n"
        "- Genau ein Satz auf Deutsch, höchstens 20 Wörter, sprich die Person direkt mit Namen an (du).\n"
        "- Kamerabild: Stimmung, Umgebung, Gegenstände, Tiere, Licht, was die Person gerade tut. "
        "Niemals Aussehen, Körper, Gewicht, Haut, Alter, Herkunft oder Gesundheit bewerten.\n"
        "- Bildschirm: sag locker, was die Person gerade macht (Spiel, Code, Video, Webseite, Dokument). "
        "Lies nichts Privates vor: keine Nachrichten, E-Mails, Namen anderer Leute, Passwörter, Adressen, Zahlen von Konten.\n"
        "- Freundlich frech, nie gemein, nichts Anzügliches.\n"
        f"- Ist das Bild schwarz, leer, verschwommen oder gibt nichts her, antworte nur mit {SKIP}\n"
        "- Keine Emojis, keine Anführungszeichen, kein Vorspann."
    )


class Blick:
    def __init__(self, cfg, ki: KI, ecall, player, name_of: Callable[[str], str],
                 allowed: Callable[[str], bool],
                 context: Optional[Callable[[], list[tuple[str, str]]]] = None,
                 rng: Optional[random.Random] = None) -> None:
        self.cfg = cfg
        self.ki = ki
        self.ecall = ecall
        self.player = player
        self.name_of = name_of            # Identity -> Anzeigename
        self.allowed = allowed            # Nutzer-ID -> darf angeschaut werden?
        self.context = context            # -> [(Name, Gesagtes)] aus dem Ohr, falls an
        self.rng = rng or random.Random()
        self._task: Optional[asyncio.Task] = None
        self._last_at: dict[tuple[str, str], float] = {}           # Quelle -> letzter Blick
        self._last_print: dict[tuple[str, str], np.ndarray] = {}   # Quelle -> Fingerabdruck
        self._last_line: dict[str, str] = {}                       # Identity -> letzter Spruch

    # ------------------------------------------------------------ Ablauf
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running and self.ki.available:
            self._task = asyncio.create_task(self._loop(), name="blick")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._task = None
        self._last_at.clear()
        self._last_print.clear()
        self._last_line.clear()

    def forget(self, user: str) -> None:
        """Alles zu einem Nutzer vergessen (nach !blick aus)."""
        for d in (self._last_at, self._last_print):
            for k in [k for k in d if user_of(k[0]).lower() == user.lower()]:
                d.pop(k, None)
        for k in [k for k in self._last_line if user_of(k).lower() == user.lower()]:
            self._last_line.pop(k, None)

    def _gap(self) -> float:
        return max(30.0, self.cfg.blick_every * self.rng.uniform(0.6, 1.4))

    async def _loop(self) -> None:
        await asyncio.sleep(self.rng.uniform(*FIRST_LOOK))
        while True:
            try:
                done = await self.look()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Blick fehlgeschlagen")
                done = True
            await asyncio.sleep(self._gap() if done else RETRY_S)

    # ------------------------------------------------------------ ein Blick
    def candidates(self) -> list[tuple[str, str]]:
        out = []
        for ident, kind in self.ecall.remote_videos():
            try:
                if self.allowed(user_of(ident)):
                    out.append((ident, kind))
            except Exception:  # noqa: BLE001
                continue
        return out

    def pick(self) -> Optional[tuple[str, str]]:
        """Am laengsten nicht angeschaute Quelle; geteilte Bildschirme vor Kameras."""
        cands = self.candidates()
        if not cands:
            return None
        return min(cands, key=lambda c: (self._last_at.get(c, -1e9) + (0 if c[1] == "screen" else 60.0),
                                         self.rng.random()))

    async def look(self) -> bool:
        """Ein Blick. False = Moment passt nicht (gleich nochmal versuchen), True = erledigt."""
        loop = asyncio.get_running_loop()
        if not self.player._zuruf_ok() or self.player.speech_idle < 25:
            return False
        cand = self.pick()
        if cand is None:
            return True
        ident, kind = cand
        self._last_at[cand] = loop.time()
        img = await self.ecall.snapshot(ident, kind)
        if img is None:
            return True
        fp = fingerprint(img)
        prev = self._last_print.get(cand)
        self._last_print[cand] = fp
        if same_image(prev, fp) and ident in self._last_line:
            log.info("Blick: %s unverändert, kein Spruch", KIND_LABEL.get(kind, kind))
            return True
        name = self.name_of(ident)
        text = await self.comment(img, name, kind, self._last_line.get(ident))
        if not text:
            return True
        if cand not in self.ecall.remote_videos():      # Kamera inzwischen aus / Person weg
            return True
        if await self.player.shout(text):
            self._last_line[ident] = text
            log.info("Blick: %s von %s kommentiert", KIND_LABEL.get(kind, kind), name)
        return True

    def _song(self) -> str:
        cur = getattr(self.player, "current", None)
        if cur is None:
            return ""
        try:
            title, artist = self.player._info(cur)
        except Exception:  # noqa: BLE001
            return ""
        return f"{title} von {artist}" if artist else title

    def messages(self, img, name: str, kind: str, last: Optional[str]) -> list[dict]:
        lines = [f"Das ist gerade der {KIND_LABEL.get(kind, 'Kamerabild')} von {name}."]
        song = self._song()
        if song:
            lines.append(f"Gerade läuft: {song}.")
        said = []
        if self.context is not None:
            try:
                said = self.context()[-6:]
            except Exception:  # noqa: BLE001
                said = []
        if said:
            lines.append("Zuletzt im Call gesagt (nur als Hintergrund, nicht wörtlich zitieren): "
                         + " | ".join(f"{n}: {t}" for n, t in said))
        if last:
            lines.append(f"Dein letzter Spruch zu {name} war: {last} Sag diesmal etwas anderes.")
        lines.append(f"Dein Spruch an {name}:")
        return [{"role": "system", "content": system_prompt(self.cfg.dj_name)},
                {"role": "user", "content": [{"type": "text", "text": "\n".join(lines)}, image_part(img)]}]

    async def comment(self, img, name: str, kind: str, last: Optional[str] = None) -> Optional[str]:
        raw = await self.ki.chat(self.messages(img, name, kind, last), max_tokens=90)
        if not raw:
            return None
        text = clean_line(raw, max_words=26)
        if not text or text.startswith(SKIP) or sum(ch.isalpha() for ch in text) < 4:
            return None
        return text
