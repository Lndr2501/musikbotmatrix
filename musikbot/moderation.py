"""DJ-Gelaber: Texte fuer Ansagen zwischen den Titeln.

Zufaellig zusammengesetzt aus Bausteinen (was lief, ein Spruch, wer sich was gewuenscht
hat, was jetzt kommt). Dieselbe Formulierung kommt nicht zweimal kurz hintereinander.
"""

from __future__ import annotations

import datetime as dt
import random
from collections import deque
from zoneinfo import ZoneInfo

AUTOPLAY = "autoplay"

WAS = [
    "Das war {prev} von {prev_artist}.",
    "Das war {prev}. Ich hoffe, euch hat das gefallen!",
    "Puh, {prev}! Was für ein Brett.",
    "Okay, {prev} ist durch.",
    "Mega, oder? Das war {prev}.",
    "{prev} – da hab ich richtig mitgewippt.",
    "Das war {prev}. Ich hab immer noch Gänsehaut. Also, Gänsefell.",
    "Applaus für {prev_artist}!",
    "So, {prev} ist vorbei. Schade eigentlich.",
]
WAS_NO_ARTIST = [w for w in WAS if "{prev_artist}" not in w]

SPRUCH = [
    "Ich hoffe, ihr habt alle was zu trinken. Ich hab meinen Kaffee.",
    "Kurzer Kaffee-Check: Becher ist noch halb voll.",
    "Hier ist euer DJ Bär, und es ist {tageszeit}.",
    "Wenn ihr Wünsche habt: Ausrufezeichen play, und los geht's.",
    "Grüße an alle im Call!",
    "Lautstärke passt? Sonst sagt Bescheid.",
    "Das ist schon Titel Nummer {nummer} heute.",
    "Bleibt dran, es wird noch besser.",
    "Ich lege hier übrigens komplett ohne Grafikkarte auf. Respekt an mich.",
    "Wer tanzt, gewinnt. Ich seh euch zwar nicht, aber ich glaub an euch.",
    "Kleiner Tipp vom Bären: Wasser trinken nicht vergessen.",
    "Die Plattenteller sind heiß heute.",
    "Ich hab Pfoten, keine Finger, und trotzdem mische ich besser als die meisten.",
    "Schön, dass ihr da seid.",
]

WUNSCH = [
    "Gewünscht von {wer}.",
    "{wer} hat sich den hier gewünscht.",
    "Danke an {wer} für den Wunsch!",
    "Der nächste geht raus an {wer}.",
]

AUTOPLAY_LINES = [
    "Ich hab da noch was Passendes gefunden.",
    "Die Warteschlange ist leer, also such ich selbst was aus.",
]

JETZT = [
    "Und jetzt kommt {next} von {next_artist}!",
    "Weiter geht's mit {next} von {next_artist}.",
    "Als Nächstes: {next}. Lautstärke hoch!",
    "Jetzt gibt's {next} auf die Ohren!",
    "Hier kommt {next}!",
    "Bühne frei für {next_artist} mit {next}!",
    "Und weiter mit {next}.",
]
JETZT_NO_ARTIST = [j for j in JETZT if "{next_artist}" not in j]

START = [
    "Hallo zusammen, hier ist DJ Bär! Wir starten mit {next}.",
    "Guten {gruss}! DJ Bär am Pult. Los geht's mit {next}!",
    "Hey! Schön, dass ihr da seid. Zum Anfang: {next}.",
]

DIREKT = [
    "Jetzt kommt: {next}!",
    "Okay, {next}. Auf geht's!",
    "Weiter mit {next}.",
    "Alles klar, {next} kommt sofort.",
]

ENDE = [
    "Das war {prev}. Und das war's erstmal von mir! Danke fürs Zuhören.",
    "{prev} war der letzte Titel. Danke, ihr wart super! Bis zum nächsten Mal.",
    "Und das war {prev}. DJ Bär macht Pause. Tschüss!",
]


def who(requester: str) -> str:
    """'@lndr:matrix.org' -> 'lndr'."""
    if not requester or requester == AUTOPLAY:
        return ""
    return requester.lstrip("@").split(":", 1)[0]


class Moderator:
    def __init__(self, tz: str = "Europe/Zurich", rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        try:
            self.tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            self.tz = None
        self.count = 0
        self._recent: deque[str] = deque(maxlen=12)

    # ---------------------------------------------------------------- Bausteine
    def _pick(self, pool: list[str]) -> str:
        fresh = [p for p in pool if p not in self._recent] or pool
        choice = self.rng.choice(fresh)
        self._recent.append(choice)
        return choice

    def _vars(self, prev=("", ""), nxt=("", "")) -> dict:
        now = dt.datetime.now(self.tz)
        h = now.hour
        tageszeit = ("mitten in der Nacht" if h < 5 else "früh am Morgen" if h < 10 else
                     "Mittag" if h < 14 else "Nachmittag" if h < 18 else "Abend" if h < 23 else "spät")
        gruss = "Morgen" if 5 <= h < 11 else "Tag" if 11 <= h < 18 else "Abend"
        tag = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][now.weekday()]
        if 18 <= h:
            tageszeit = f"{tag}abend"
        return {"prev": prev[0], "prev_artist": prev[1], "next": nxt[0], "next_artist": nxt[1],
                "tageszeit": tageszeit, "gruss": gruss, "nummer": self.count}

    # ---------------------------------------------------------------- Texte
    def between(self, prev: tuple[str, str], nxt: tuple[str, str], requester: str = "") -> str:
        """Pause zwischen zwei Titeln: was lief, vielleicht ein Spruch, Wunsch, was kommt."""
        self.count += 1
        v = self._vars(prev, nxt)
        parts = [self._pick(WAS if prev[1] else WAS_NO_ARTIST)]
        if self.rng.random() < 0.45:
            parts.append(self._pick(SPRUCH))
        name = who(requester)
        if requester == AUTOPLAY:
            parts.append(self._pick(AUTOPLAY_LINES))
        elif name and self.rng.random() < 0.7:
            parts.append(self._pick(WUNSCH).replace("{wer}", name))
        parts.append(self._pick(JETZT if nxt[1] else JETZT_NO_ARTIST))
        return " ".join(p.format(**v) for p in parts)

    def start(self, nxt: tuple[str, str], first: bool) -> str:
        """Titel per !play gestartet: kurz ueber das Intro sprechen."""
        self.count += 1
        v = self._vars(nxt=nxt)
        return self._pick(START if first else DIREKT).format(**v)

    def end(self, prev: tuple[str, str]) -> str:
        return self._pick(ENDE).format(**self._vars(prev=prev))
