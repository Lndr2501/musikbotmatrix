"""Reine Cache-/Speicherlogik – bewusst ohne I/O, dadurch gut testbar.

Regeln (aus der Anforderung):
1. Ein Titel, der insgesamt ``save_after``-mal angefordert wurde, wird dauerhaft
   in die Library verschoben und nie wieder automatisch gelöscht.
2. Im Cache liegen höchstens ``cache_max`` Titel.
3. Ein Titel, der neu in den Cache kommt, muss ``cache_window_self`` weitere
   Anforderungen sammeln, bevor ``cache_window_others`` *andere* Titel
   angefordert wurden – sonst fliegt er raus.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CACHE = "cache"
LIBRARY = "library"


@dataclass
class Entry:
    key: str
    state: str = CACHE
    requests: int = 0
    win_self: int = 0
    win_others: int = 0
    settled: bool = False
    last_seen: int = 0


@dataclass(frozen=True)
class Policy:
    save_after: int = 3
    cache_max: int = 10
    window_self: int = 2
    window_others: int = 9

    @classmethod
    def from_config(cls, cfg) -> "Policy":
        return cls(
            save_after=cfg.save_after_requests,
            cache_max=cfg.cache_max_entries,
            window_self=cfg.cache_window_self,
            window_others=cfg.cache_window_others,
        )


@dataclass
class Outcome:
    """Was eine Anforderung ausgelöst hat."""

    entry: Entry
    first_request: bool = False
    promoted: bool = False  # umgezogen in die Library
    evicted: list[str] = field(default_factory=list)  # löschreife Keys
    removed: list[Entry] = field(default_factory=list)  # die Entries selbst (für Dateilöschung)
    protected: bool = False  # Fenster gerade erfüllt


def _cache_entries(entries: dict[str, Entry]) -> list[Entry]:
    return [e for e in entries.values() if e.state == CACHE]


def register_request(entries: dict[str, Entry], key: str, policy: Policy, now: int = 0) -> Outcome:
    """Bucht eine Anforderung von ``key`` und wendet alle Regeln an."""
    entry = entries.get(key)
    first = entry is None
    if entry is None:
        entry = Entry(key=key, state=CACHE, requests=1, last_seen=now)
        entries[key] = entry

    entry.last_seen = now

    if entry.state == CACHE:
        entry.requests += 0 if first else 1
        if not entry.settled:
            entry.win_self += 1
            if entry.win_self >= policy.window_self:
                entry.settled = True
    else:
        entry.requests += 0 if first else 1

    protected = bool(entry.settled)

    # Regel 3: alle *anderen* Cache-Titel rutschen ein Stück Richtung Rauswurf.
    doomed: list[Entry] = []
    for other in _cache_entries(entries):
        if other.key == key or other.settled:
            continue
        other.win_others += 1
        if other.win_others >= policy.window_others:
            other.state = "evicted"
            doomed.append(other)

    # Regel 1: oft genug angefordert -> dauerhaft speichern.
    promoted = False
    if entry.state == CACHE and entry.requests >= policy.save_after:
        entry.state = LIBRARY
        promoted = True
        doomed = [e for e in doomed if e.key != key]

    # Regel 2: Cache-Größe begrenzen.
    cache = _cache_entries(entries)
    while len(cache) > policy.cache_max:
        worst = sorted(
            cache,
            key=lambda e: (
                e.settled,           # ungeschützte zuerst raus
                -e.win_others,       # die kurz vorm Rauswurf zuerst
                e.last_seen,         # dann die am längsten unberührten
            ),
        )[0]
        worst.state = "evicted"
        doomed.append(worst)
        cache = _cache_entries(entries)

    for dead in doomed:
        entries.pop(dead.key, None)

    return Outcome(entry=entry, first_request=first, promoted=promoted,
                   evicted=[e.key for e in doomed], removed=doomed, protected=protected)
