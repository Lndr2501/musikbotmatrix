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
    "seek": ("seek", "spule"),
    "shuffle": ("shuffle", "mischen"),
    "loop": ("loop", "wiederholen"),
    "joincall": ("joincall", "call", "anruf", "beitreten", "reinkommen"),
    "leavecall": ("leavecall", "leave", "auflegen", "raus"),
    "cache": ("cache", "caches", "tmp"),
    "library": ("library", "saved", "bibliothek", "gespeichert"),
    "unsave": ("unsave", "vergiss", "forget"),
    "video": ("video", "bild"),
    "help": ("help", "hilfe", "commands", "befehle"),
}

_LOOKUP: dict[str, str] = {}
for _canon, _names in ALIASES.items():
    for _n in _names:
        _LOOKUP.setdefault(_n, _canon)

COMMANDS: tuple[str, ...] = tuple(sorted(ALIASES))

HELP = """Kommandos:
/play <Link oder Suchbegriff> – Titel abspielen (Suche, wenn keine URL)
/pause – pausieren · /resume – weiter
/stop – stoppen · /skip – nächster Titel · /previous – vorheriger
/queue – Warteschlange · /clear – Warteschlange leeren
/status – was läuft gerade? · /seek <mm:ss oder ±s> – spulen
/volume <0-100> – Lautstärke · /video on|off – Video im Call
/shuffle – mischen · /loop on|off – aktuelle Titel wiederholen
/joincall – Bot ruft alle im Raum an (Mesh) · /leavecall – auflegen
/cache – Cache-Status · /library – gespeicherte Titel · /unsave <Nr.> – aus Bibliothek entfernen
/hilfe – diese Liste
Regel: Titel ab 3 Anforderungen dauerhaft gespeichert, sonst Cache
(max. 10; ein Titel fällt raus, wenn er 2 Anforderungen braucht, bevor
9 andere liefen). Alles über .env konfigurierbar."""


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
