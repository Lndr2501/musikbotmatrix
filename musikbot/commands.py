"""Befehls-Parsing: Text -> (Name, Argument). Rein, gut testbar."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Kanonischer Name -> Aliase (alle klein geschrieben).
ALIASES: dict[str, tuple[str, ...]] = {
    "play": ("play", "p", "abspielen", "spiele", "song"),
    "pause": ("pause", "pausieren", "halt"),
    "resume": ("resume", "continue", "fortsetzen", "weiter"),
    "stop": ("stop", "stopp", "beenden"),
    "skip": ("skip", "next", "naechster", "weiter", "ueberspringen",
             "überspringen"),
    "previous": ("previous", "prev", "vorheriger"),
    "queue": ("queue", "liste", "warteschlange"),
    "clear": ("clear", "leeren"),
    "status": ("status", "now", "playing", "info", "was"),
    "volume": ("volume", "vol", "lautstaerke", "lautstärke", "leise", "laut"),
    "seek": ("seek", "spule", "goto"),
    "forward": ("forward", "ff", "vor", "vorspulen", "fwd"),
    "rewind": ("rewind", "rw", "zurueck", "zurück", "rueckspulen", "rückspulen", "back"),
    "shuffle": ("shuffle", "mischen"),
    "loop": ("loop", "wiederholen"),
    "joincall": ("joincall", "call", "anruf", "beitreten", "reinkommen"),
    "leavecall": ("leavecall", "leave", "auflegen", "raus"),
    "video": ("video", "bild"),
    "playlist": ("playlist", "playlists", "pl", "wiedergabeliste"),
    "fav": ("fav", "favs", "favorit", "favoriten", "favorite", "favorites", "merken"),
    "stats": ("stats", "statistik", "top", "charts"),
    "always": ("always", "247", "24/7", "24-7", "dauerbetrieb"),
    "help": ("help", "hilfe", "commands", "befehle"),
}

_LOOKUP: dict[str, str] = {}
for _canon, _names in ALIASES.items():
    for _n in _names:
        _LOOKUP.setdefault(_n, _canon)

COMMANDS: tuple[str, ...] = tuple(sorted(ALIASES))

HELP = """Kommandos:
!play <Link oder Suchbegriff> – Link von praktisch jeder Video-/Audioseite (yt-dlp), sonst YouTube-Suche
    Suche woanders: !play sc: <Begriff> (SoundCloud) · bili: · nico:
Audio-/Videodatei an die Nachricht anhängen – der Bot spielt sie direkt ab
!pause – pausieren · !resume – weiter
!stop – stoppen · !skip – nächster Titel · !previous – vorheriger
!queue – Warteschlange · !clear – Warteschlange leeren
!status – was läuft gerade? · !seek <mm:ss | +30 | -30> – springen
!vor [s] – vorspulen (Standard 10 s) · !zurueck [s] – zurückspulen
!volume <0-300 | +10 | -10> – Lautstärke (60 = Original, darüber Boost) · !video on|off – Video im Call
!shuffle – mischen · !loop on|off – aktuelle Titel wiederholen
!joincall – Bot kommt in den Call · !leavecall – Bot verlässt den Call
!playlist create|delete <name> · add <name> [Link] · remove <name> <nr>
    show <name> · list · play <name> [shuffle] · enqueue <name>
    (ohne Link bei add: der laufende Titel)
!fav – laufenden Titel merken · !fav list · !fav remove <nr> · !fav play [shuffle] (deine Favoriten)
!stats – meistgespielte Titel und wer am meisten Musik hört
!24/7 (oder !24/7 radio) – zufällig die MP3s aus dem Radio-Ordner, Dauerschleife
!24/7 song [Link] · !24/7 playlist <name> · !24/7 off – Dauerschleife, Bot bleibt im Call
!hilfe – diese Liste
Steuern dürfen nur Leute, die selbst im Call sind (Lesen: help, status, queue,
playlist list/show geht immer). Ist die Warteschlange leer und der Song zu Ende,
verlässt der Bot den Call (außer im 24/7-Modus).
Hinweis: Titel werden nur gespeichert, wenn sie oft gespielt werden (ab 5 Downloads);
nach 3 Tagen ohne Wiedergabe wird die Datei wieder gelöscht."""


@dataclass
class Parsed:
    name: str
    arg: str = ""


def parse(text: str, prefixes: tuple[str, ...] = ("/",)) -> Optional[Parsed]:
    text = (text or "").strip()
    if not text:
        return None
    prefix = next((p for p in prefixes if text.startswith(p)), None)
    if not prefix:
        return None
    rest = text[len(prefix):].strip()
    if not rest:
        return None
    head, _, tail = rest.partition(" ")
    name = _LOOKUP.get(head.lower())
    if name is None:
        return None
    return Parsed(name, tail.strip())


_READ_ONLY = {"help", "status", "queue", "stats"}


def is_read_only(name: str, arg: str = "") -> bool:
    """True, wenn der Befehl den Bot nicht steuert (darf jeder im Raum)."""
    if name in _READ_ONLY:
        return True
    sub = arg.strip().split(" ", 1)[0].lower()
    if name == "playlist":
        return sub in ("", "list", "show", "liste", "zeige")
    if name == "fav":                      # merken/ansehen darf jeder, abspielen steuert
        return sub != "play"
    if name == "always":
        return sub == "status"
    return False
