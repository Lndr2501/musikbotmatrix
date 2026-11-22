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

# Wie der letzte Song war (aus der Audio-Analyse des Baeren)
WAS_HOT = [
    "Puh, {prev}! Was für ein Brett.",
    "{prev}! Meine Pfoten qualmen noch.",
    "Was für eine Energie bei {prev}! Ich bin ganz außer Atem.",
]
WAS_FAST = [
    "{prev} mit {bpm} Beats pro Minute. Da musste ich ganz schön strampeln.",
    "{bpm} Beats pro Minute! {prev} war richtig schnell.",
]
WAS_DROPS = [
    "{drops} Drops in einem Song! Ich hab mitgezählt.",
    "{prev}: {drops} Drops. Mein Herz hält das kaum aus.",
]
WAS_CALM = [
    "Schön entspannt war das: {prev}.",
    "{prev}, zum Dahinschmelzen.",
    "Das war {prev}. So schön ruhig. Ich wär fast eingeschlafen. Fast!",
]
WAS_BASS = [
    "Der Bass bei {prev}! Mein Fell vibriert immer noch.",
]

# Zurufe mitten im Song (Musik wird kurz leiser)
ZURUF_NAME = [
    "{name}, du bist geil!",
    "Shoutout an {name}!",
    "Grüße gehen raus an {name}!",
    "{name}, du hast einen super Musikgeschmack!",
    "Hey {name}, alles klar bei dir?",
    "Das hier ist für dich, {name}!",
    "{name}, bester Mensch im Call!",
    "{name}, ich hab dich lieb! Also, rein platonisch.",
    "{name}, du siehst heute übrigens fantastisch aus!",
    "Applaus für {name}!",
]
ZURUF_NAME_HYPE = [
    "{name}, ich seh dich tanzen!",
    "{name}, hör auf zu arbeiten und tanz!",
    "Wo ist {name}? Ah, da! Hände hoch!",
    "{name}, lauter! Ich hör dich nicht!",
    "{name}, zeig mal deine besten Moves!",
]
ZURUF_NAME_CALM = [
    "{name}, lehn dich zurück und genieß das.",
    "Hey {name}, schön, dass du da bist.",
    "{name}, der hier ist zum Entspannen. Für dich.",
]
ZURUF = [
    "Hände hoch!",
    "Lauter!",
    "Geht ab hier!",
    "Ist das ein Brett oder was?",
    "Ich liebe diesen Track!",
    "Wer tanzt mit?",
    "Jaaa, Leute!",
    "Party im Call!",
    "Kaffee hoch, wer mitfeiert!",
    "Ich krieg Gänsefell!",
    "Alle zusammen jetzt!",
    "Was für eine Stimmung hier!",
]
DROP = [
    "Wooo! Was für ein Drop!",
    "Da ist der Drop!",
    "Bäm! Genau so!",
    "Jetzt geht's richtig los!",
]
DROP_NAME = [
    "{name}, hast du den Drop gehört?",
    "Für dich, {name}! Bäm!",
]
# je nach Stelle im Song
BUILD = [
    "Achtung, gleich kommt's!",
    "Spürt ihr das? Da baut sich was auf!",
    "Macht euch bereit!",
    "Drei, zwei, eins …",
    "Gleich knallt's, haltet euch fest!",
]
BREAK = [
    "Kurz durchatmen.",
    "Und jetzt alle mal die Arme schwingen.",
    "Genießt die Ruhe, gleich geht's weiter.",
    "Kleine Pause für die Pfoten.",
]
CALM = [
    "Ganz entspannt hier. Ich mag's.",
    "Schön chillig gerade.",
    "Perfekt zum Zurücklehnen.",
    "Bärenkuschel-Musik. Sehr gut.",
]
FAST = [
    "{bpm} Beats pro Minute! Meine Pfoten glühen!",
    "Ist das schnell! Ich komm kaum hinterher.",
    "Vollgas, Leute!",
    "Das Tempo hier, Wahnsinn!",
]
BASS = [
    "Hört ihr diesen Bass?",
    "Der Bass drückt richtig!",
    "Was für ein Bass! Mein Fell vibriert.",
]
SONG = [
    "Ich liebe {title}!",
    "{title}, was für ein Song!",
    "Wer kann {title} mitsingen?",
]
SONG_ARTIST = [
    "{artist} geht einfach immer!",
    "Danke, {artist}, für diesen Song!",
]

ENDE = [
    "Das war {prev}. Und das war's erstmal von mir! Danke fürs Zuhören.",
    "{prev} war der letzte Titel. Danke, ihr wart super! Bis zum nächsten Mal.",
    "Und das war {prev}. DJ Bär macht Pause. Tschüss!",
]


ZAHLEN = ["null", "einen", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn"]


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
    def _recap(self, song: dict | None) -> list[str] | None:
        """Passender Satz-Pool zum letzten Song (schnell, wild, ruhig, viele Drops) oder None."""
        if not song or song.get("seconds", 0) < 20:
            return None
        energy, drops, bpm = song.get("energy", 0.5), song.get("drops", 0), song.get("bpm", 0)
        pools = []
        if drops >= 3:
            pools.append(WAS_DROPS)
        if bpm >= 128 and energy > 0.55:
            pools.append(WAS_FAST)
        if energy > 0.72:
            pools.append(WAS_HOT)
        if energy < 0.45:
            pools.append(WAS_CALM)
        if song.get("bass", 0) > 0.38 and energy > 0.5:
            pools.append(WAS_BASS)
        return self.rng.choice(pools) if pools else None

    def between(self, prev: tuple[str, str], nxt: tuple[str, str], requester: str = "",
                song: dict | None = None) -> str:
        """Pause zwischen zwei Titeln: was lief (gern dazu, wie er war), vielleicht ein Spruch,
        Wunsch, was kommt. `song`: Beat.song() des letzten Titels."""
        self.count += 1
        v = self._vars(prev, nxt)
        if song:
            v["bpm"] = str(round(song.get("bpm") or 0))
            n = int(song.get("drops", 0))
            v["drops"] = ZAHLEN[n].capitalize() if n < len(ZAHLEN) else str(n)
        recap = self._recap(song)
        parts = [self._pick(recap if recap and self.rng.random() < 0.7 else
                            WAS if prev[1] else WAS_NO_ARTIST)]
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

    def zuruf(self, names: list[str], drop: bool = False, vibe: dict | None = None,
              song: tuple[str, str] = ("", "")) -> str:
        """Kurzer Zuruf mitten im Song; oft an jemanden aus dem Call. `vibe` (Beat.state) macht
        ihn passend: Build-up ankuendigen, im Break durchatmen, bei schnellen Songs das Tempo,
        bei ruhigen nichts Wildes. `song`: (Titel, Interpret)."""
        name = self.rng.choice(names) if names else ""
        v = vibe or {}
        energy = v.get("energy", 0.6)
        section = v.get("section", "groove")
        bpm = v.get("bpm") or 0.0
        fill = {"{name}": name, "{bpm}": str(round(bpm)), "{title}": song[0], "{artist}": song[1]}

        def say(pool: list[str]) -> str:
            text = self._pick(pool)
            for key, val in fill.items():
                text = text.replace(key, val)
            return text

        if drop:
            return say(DROP_NAME if name and self.rng.random() < 0.3 else DROP)
        if section == "build" and self.rng.random() < 0.8:
            return say(BUILD)
        if section == "break" and self.rng.random() < 0.7:
            return say(BREAK)
        calm = section in ("calm", "break") or energy < 0.4
        options: list[tuple[float, list[str]]] = []
        if name:
            options.append((1.2, ZURUF_NAME))
            options.append((0.8, ZURUF_NAME_CALM if calm else ZURUF_NAME_HYPE if energy > 0.6 else ZURUF_NAME))
        if calm:
            options.append((1.0, CALM))
        else:
            options.append((1.0, ZURUF))
            if bpm >= 126 and energy > 0.6:
                options.append((0.6, FAST))
            if v.get("low", 0.0) >= 0.38:
                options.append((0.5, BASS))
        if song[0] and vibe is not None:
            options.append((0.5, SONG_ARTIST if song[1] and self.rng.random() < 0.5 else SONG))
        weights, pools = zip(*options)
        return say(self.rng.choices(pools, weights)[0])

    def end(self, prev: tuple[str, str], song: dict | None = None) -> str:
        v = self._vars(prev=prev)
        recap = self._recap(song)
        if recap is not None and recap is not WAS_DROPS and recap is not WAS_FAST and self.rng.random() < 0.6:
            first = self._pick(recap).format(**v, bpm="", drops="")
            return first + " Und das war's erstmal von mir! Danke fürs Zuhören."
        return self._pick(ENDE).format(**v)
