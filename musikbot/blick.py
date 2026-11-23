"""Blick: der DJ schaut ab und zu auf alle Kameras und geteilten Bildschirme im Call
und ruft einen kurzen Spruch dazu rein.

Von selbst alle paar Minuten, oder sofort per `!blick` (`look_now`). Ablauf: von allen Quellen gleichzeitig ein Standbild (`ElementCall.snapshot`, kurzes
Abo), stark verkleinert und komprimiert, alle zusammen in EINER Anfrage ans Bildmodell -> ein
Satz -> `Player.shout`. Das ist schneller als reihum (ein Aufruf statt vieler) und er sieht
trotzdem alle. Bilder werden nicht gespeichert; pro Quelle bleibt nur ein 16x9-Graustufen-
Fingerabdruck, damit Unveraendertes nicht dauernd kommentiert wird.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from typing import Callable, Optional

import numpy as np

from .ki import KI, clean_line, image_part
from .publikum import user_of

log = logging.getLogger("musikbot.blick")

FIRST_LOOK = (35.0, 70.0)      # erster Blick nach dem Beitritt (s)
RETRY_S = 12.0                 # Moment passt gerade nicht (Ansage, Songanfang): bald wieder
SAME_IMAGE = 7.0               # mittlere Grauwert-Differenz darunter = gleiches Bild
MAX_SOURCES = 6                # hoechstens so viele Bilder pro Anfrage (Bildschirme zuerst)
SNAP_TIMEOUT = 8.0             # pro Standbild; alle laufen gleichzeitig
# Klein und stark komprimiert: wenige Bild-Tokens = schnelle Antwort. Bildschirme etwas groesser (Text).
SIDE = {"camera": 448, "screen": 640}
QUALITY = 55
SKIP = "-"                     # Antwort des Modells: nichts Sinnvolles zu sehen
SAY_WAIT = 20.0                # manuell: so lange auf eine Luecke zum Reinrufen warten, sonst Chat

KIND_LABEL = {"camera": "Kamerabild", "screen": "geteilter Bildschirm"}


def fingerprint(img) -> np.ndarray:
    return np.asarray(img.convert("L").resize((16, 9)), dtype=np.float32)


def same_image(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> bool:
    return a is not None and b is not None and float(np.abs(a - b).mean()) < SAME_IMAGE


def system_prompt(dj: str) -> str:
    return (
        f"Du bist {dj}, ein frecher, herzlicher Plüschbär-DJ in einem Videocall unter Freunden. "
        "Du legst Musik auf und wirfst ab und zu einen kurzen Spruch über das rein, was du bei den Leuten siehst. "
        "Du bekommst die aktuellen Standbilder aller Kameras und geteilten Bildschirme, jeweils mit Namen.\n"
        "Regeln:\n"
        "- Such dir das Interessanteste aus (am liebsten etwas, das als neu markiert ist) und mach genau "
        "einen Satz auf Deutsch, höchstens 20 Wörter. Sprich die Person (oder die Leute) direkt mit Namen an.\n"
        "- Kamerabild: Stimmung, Umgebung, Gegenstände, Tiere, Licht, was jemand gerade tut. "
        "Niemals Aussehen, Körper, Gewicht, Haut, Alter, Herkunft oder Gesundheit bewerten.\n"
        "- Bildschirm: sag locker, was die Person gerade macht (Spiel, Code, Video, Webseite, Dokument). "
        "Lies nichts Privates vor: keine Nachrichten, E-Mails, Namen anderer Leute, Passwörter, Adressen, Kontodaten.\n"
        "- Freundlich frech, nie gemein, nichts Anzügliches.\n"
        f"- Geben die Bilder nichts her (schwarz, leer, verschwommen), antworte nur mit {SKIP}\n"
        "- Keine Emojis, keine Anführungszeichen, kein Vorspann."
    )


class Blick:
    def __init__(self, cfg, ki: KI, ecall, player, name_of: Callable[[str], str],
                 allowed: Callable[[str], bool],
                 rng: Optional[random.Random] = None) -> None:
        self.cfg = cfg
        self.ki = ki
        self.ecall = ecall
        self.player = player
        self.name_of = name_of            # Identity -> Anzeigename
        self.allowed = allowed            # Nutzer-ID -> darf angeschaut werden?
        self.rng = rng or random.Random()
        self._task: Optional[asyncio.Task] = None
        self._last_print: dict[tuple[str, str], np.ndarray] = {}   # Quelle -> Fingerabdruck
        self._lines: deque[str] = deque(maxlen=4)                  # letzte Sprueche
        self._lock = asyncio.Lock()                                # ein Blick zur Zeit (Schleife/manuell)

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
        self._last_print.clear()
        self._lines.clear()

    def forget(self, user: str) -> None:
        """Alles zu einem Nutzer vergessen (nach !blick aus)."""
        for k in [k for k in self._last_print if user_of(k[0]).lower() == user.lower()]:
            self._last_print.pop(k, None)

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
    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def candidates(self, match: Optional[Callable[[str], bool]] = None) -> list[tuple[str, str]]:
        """Alle erlaubten Quellen (optional nur die, auf deren Identity `match` passt),
        geteilte Bildschirme zuerst, hoechstens MAX_SOURCES."""
        out = []
        for ident, kind in self.ecall.remote_videos():
            try:
                if self.allowed(user_of(ident)) and (match is None or match(ident)):
                    out.append((ident, kind))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda c: c[1] != "screen")
        return out[:MAX_SOURCES]

    async def look(self) -> bool:
        """Ein Blick auf alle. False = Moment passt nicht (gleich nochmal versuchen), True = erledigt."""
        if not self.player._zuruf_ok() or self.player.speech_idle < 25:
            return False
        if self.busy:                                    # gerade manuell geschaut
            return True
        async with self._lock:
            return await self._look_auto()

    async def _snap(self, cands: list) -> list:
        shots = await asyncio.gather(*(self.ecall.snapshot(i, k, timeout=SNAP_TIMEOUT) for i, k in cands),
                                     return_exceptions=True)
        return [(c, img) for c, img in zip(cands, shots) if img is not None and not isinstance(img, BaseException)]

    async def _look_auto(self) -> bool:
        cands = self.candidates()
        if not cands:
            return True
        got = await self._snap(cands)
        if not got:
            return True
        new = set()
        for c, img in got:
            fp = fingerprint(img)
            if not same_image(self._last_print.get(c), fp):
                new.add(c)
            self._last_print[c] = fp
        if not new and self._lines:
            log.info("Blick: %d Bilder unverändert, kein Spruch", len(got))
            return True
        text = await self.comment(got, new)
        if not text:
            return True
        live = set(self.ecall.remote_videos())
        if not any(c in live for c, _ in got):          # inzwischen alle Kameras aus / Leute weg
            return True
        if await self.player.shout(text):
            self._lines.append(text)
            log.info("Blick: %d Bilder angeschaut, Spruch gesagt", len(got))
        return True

    async def look_now(self, match: Optional[Callable[[str], bool]] = None) -> tuple[str, Optional[str]]:
        """Manuell (`!blick`): sofort hinschauen, auch auf unveraenderte Bilder.
        -> (Ergebnis, Spruch). Ergebnis: "busy", "nobody", "noimage", "nothing",
        "said" (ueber die Musik gesagt) oder "text" (gerade keine Luecke/keine Musik: Spruch fuer den Chat)."""
        if self.busy:
            return "busy", None
        async with self._lock:
            cands = self.candidates(match)
            if not cands:
                return "nobody", None
            got = await self._snap(cands)
            if not got:
                return "noimage", None
            for c, img in got:
                self._last_print[c] = fingerprint(img)
            text = await self.comment(got, {c for c, _ in got})
            if not text:
                return "nothing", None
            self._lines.append(text)
            said = await self._say_soon(text)
            log.info("Blick (manuell): %d Bilder angeschaut, %s", len(got), "gesagt" if said else "in den Chat")
            return ("said" if said else "text"), text

    async def _say_soon(self, text: str) -> bool:
        """Reinrufen, sobald es passt (hoechstens SAY_WAIT s warten). Ohne laufende Musik gar nicht."""
        loop = asyncio.get_running_loop()
        end = loop.time() + SAY_WAIT
        while getattr(self.player, "state", "playing") == "playing":
            if self.player._zuruf_ok():
                return await self.player.shout(text)     # ein Versuch (Sprachsynthese kostet)
            if loop.time() >= end:
                break
            await asyncio.sleep(0.5)
        return False

    def _song(self) -> str:
        cur = getattr(self.player, "current", None)
        if cur is None:
            return ""
        try:
            title, artist = self.player._info(cur)
        except Exception:  # noqa: BLE001
            return ""
        return f"{title} von {artist}" if artist else title

    def messages(self, got: list, new: set) -> list[dict]:
        intro = ["Die aktuellen Bilder aus dem Call:"]
        song = self._song()
        if song:
            intro.insert(0, f"Gerade läuft: {song}.")
        content: list[dict] = [{"type": "text", "text": "\n".join(intro)}]
        for n, ((ident, kind), img) in enumerate(got, 1):
            mark = " (neu)" if (ident, kind) in new else ""
            content.append({"type": "text",
                            "text": f"Bild {n}: {KIND_LABEL.get(kind, 'Kamerabild')} von {self.name_of(ident)}{mark}"})
            content.append(image_part(img, max_side=SIDE.get(kind, 448), quality=QUALITY))
        tail = []
        if self._lines:
            tail.append("Deine letzten Sprüche (sag etwas anderes): " + " | ".join(self._lines))
        tail.append("Dein Spruch:")
        content.append({"type": "text", "text": "\n".join(tail)})
        return [{"role": "system", "content": system_prompt(self.cfg.dj_name)},
                {"role": "user", "content": content}]

    async def comment(self, got: list, new: set) -> Optional[str]:
        raw = await self.ki.chat(self.messages(got, new), max_tokens=90)
        if not raw:
            return None
        text = clean_line(raw, max_words=26)
        if not text or text.startswith(SKIP) or sum(ch.isalpha() for ch in text) < 4:
            return None
        return text
