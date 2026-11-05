"""SQLite-Speicher für Titel, Wiedergabe-Warteschlange und Cache-Policy.

Ein Titel ist genau einmal in der Datenbank und kennt seinen Zustand:
``cache`` (darf wieder wegfliegen) oder ``library`` (bleibt für immer).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable

from .policy import CACHE, LIBRARY, Entry, Outcome, Policy, register_request
from .util import now_int

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    duration    REAL NOT NULL DEFAULT 0,
    uploader    TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'cache',
    requests    INTEGER NOT NULL DEFAULT 0,
    win_self    INTEGER NOT NULL DEFAULT 0,
    win_others  INTEGER NOT NULL DEFAULT 0,
    settled     INTEGER NOT NULL DEFAULT 0,
    added_at    INTEGER NOT NULL DEFAULT 0,
    last_seen   INTEGER NOT NULL DEFAULT 0,
    rel_path    TEXT NOT NULL DEFAULT '',
    bytes       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queue (
    pos        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key  TEXT NOT NULL,
    requester  TEXT NOT NULL DEFAULT '',
    added_at   INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Track(Entry):
    title: str = ""
    url: str = ""
    duration: float = 0.0
    uploader: str = ""
    added_at: int = 0
    rel_path: str = ""
    bytes: int = 0

    @property
    def playable(self) -> bool:
        return bool(self.rel_path) and os.path.exists(self.abs_path)

    @property
    def abs_path(self) -> str:
        return self._abs

    _abs: str = ""


class TrackStore:
    def __init__(self, db_path: str, policy: Policy, cache_dir: str, library_dir: str) -> None:
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(library_dir, exist_ok=True)
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.policy = policy
        self.cache_dir = cache_dir
        self.library_dir = library_dir
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._tracks: dict[str, Track] = {}
        self._load()

    # ------------------------------------------------------------- laden
    def _load(self) -> None:
        with self._lock:
            self._tracks.clear()
            for row in self.db.execute("SELECT * FROM tracks"):
                track = Track(
                    key=row["key"],
                    state=row["state"],
                    requests=row["requests"],
                    win_self=row["win_self"],
                    win_others=row["win_others"],
                    settled=bool(row["settled"]),
                    last_seen=row["last_seen"],
                    title=row["title"],
                    url=row["url"],
                    duration=row["duration"],
                    uploader=row["uploader"],
                    added_at=row["added_at"],
                    rel_path=row["rel_path"],
                    bytes=row["bytes"],
                )
                track._abs = os.path.join(self._dir_for_state(track.state), track.rel_path) \
                    if track.rel_path else ""
                self._tracks[track.key] = track
            log.info("Speicher geladen: %d Library-Titel, %d Cache-Titel",
                     self.count(LIBRARY), self.count(CACHE))

    def _dir_for_state(self, state: str) -> str:
        return self.library_dir if state == LIBRARY else self.cache_dir

    # ------------------------------------------------------- Buchhaltung
    def note_request(self, key: str, *, title: str = "", url: str = "",
                     duration: float = 0.0, uploader: str = "",
                     now: int | None = None) -> tuple[Track, Outcome]:
        """Bucht eine Anforderung und räumt Dateien auf, die die Policy killt."""
        now = now or now_int()
        with self._lock:
            track = self._tracks.get(key)
            if track is None:
                track = Track(key=key, added_at=now)
                self._tracks[key] = track
            if title:
                track.title = title
            if url:
                track.url = url
            if duration:
                track.duration = duration
            if uploader:
                track.uploader = uploader

            outcome = register_request(self._tracks, key, self.policy, now)
            track = self._tracks[key]

            if outcome.promoted:
                self._move_file(track, LIBRARY)
            for dead in outcome.removed:
                self._remove_files(dead)

            self._persist(track)
            for dead in outcome.evicted:
                self.db.execute("DELETE FROM tracks WHERE key = ?", (dead,))
                self.db.execute("DELETE FROM queue WHERE track_key = ?", (dead,))
            self.db.commit()
            if outcome.evicted:
                log.info("Aus dem Cache geworfen: %s", ", ".join(outcome.evicted))
            if outcome.promoted:
                log.info("Dauerhaft gespeichert: %s", track.title or track.key)
            return track, outcome

    def _move_file(self, track: Track, state: str) -> None:
        if not track.rel_path:
            track._abs = ""
            return
        old = os.path.join(self._dir_for_state("cache" if state == LIBRARY else state), track.rel_path)
        new_dir = self._dir_for_state(state)
        new = os.path.join(new_dir, track.rel_path)
        if os.path.exists(old):
            os.makedirs(new_dir, exist_ok=True)
            os.replace(old, new)
            track._abs = new
        else:
            track._abs = new if os.path.exists(new) else ""

    def _remove_files(self, track: Track) -> None:
        if not track.rel_path:
            return
        for directory in (self.cache_dir, self.library_dir):
            path = os.path.join(directory, track.rel_path)
            try:
                os.remove(path)
            except FileNotFoundError:
                log.debug("Datei %s war schon weg", path)

    def set_file(self, key: str, path: str) -> None:
        """Nach dem Download: Dateipfad eintragen (relativ zum jeweiligen Ordner)."""
        with self._lock:
            track = self._tracks[key]
            filename = os.path.basename(path)
            track.rel_path = filename
            track.bytes = os.path.getsize(path) if os.path.exists(path) else 0
            track._abs = path
            self._persist(track)
            self.db.commit()

    def dir_for(self, key: str) -> str:
        with self._lock:
            track = self._tracks.get(key)
            state = track.state if track else CACHE
            return self._dir_for_state(state)

    def forget(self, key: str) -> bool:
        """Titel absichtlich löschen (inkl. Library)."""
        with self._lock:
            if key not in self._tracks:
                return False
            self._delete_files(key)
            self._tracks.pop(key)
            self.db.execute("DELETE FROM tracks WHERE key = ?", (key,))
            self.db.execute("DELETE FROM queue WHERE track_key = ?", (key,))
            self.db.commit()
            return True

    # ------------------------------------------------------------ Abfragen
    def get(self, key: str) -> Track | None:
        with self._lock:
            return self._tracks.get(key)

    def all(self) -> list[Track]:
        with self._lock:
            return list(self._tracks.values())

    def by_state(self, state: str) -> list[Track]:
        with self._lock:
            return [t for t in self._tracks.values() if t.state == state]

    def count(self, state: str) -> int:
        return len(self.by_state(state))

    def cache_used_bytes(self) -> int:
        return sum(t.bytes for t in self.by_state(CACHE))

    def library_used_bytes(self) -> int:
        return sum(t.bytes for t in self.by_state(LIBRARY))

    def cache_report(self) -> list[tuple[Track, str]]:
        """Cache-Titel inkl. Menschen-lesbarem Status des Fensters."""
        rows: list[tuple[Track, str]] = []
        for track in self.by_state(CACHE):
            if track.settled:
                status = "geschützt"
            else:
                status = (f"{track.win_self}/{self.policy.window_self} Anfragen in "
                          f"{track.win_others}/{self.policy.window_others} Fensterschritten")
            rows.append((track, status))
        return rows

    # ---------------------------------------------------------- Warteschlange
    def queue_add(self, key: str, requester: str = "") -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO queue (track_key, requester, added_at) VALUES (?,?,?)",
                (key, requester, now_int()),
            )
            self.db.commit()

    def queue_all(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT track_key, requester FROM queue ORDER BY pos ASC").fetchall()
            return [(r["track_key"], r["requester"]) for r in rows]

    def queue_pop(self) -> tuple[str, str] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM queue ORDER BY pos ASC LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM queue WHERE pos = ?", (row["pos"],))
            self.db.commit()
            return row["track_key"], row["requester"]

    def queue_clear(self) -> int:
        with self._lock:
            rows = self.db.execute("SELECT COUNT(*) AS c FROM queue").fetchone()
            self.db.execute("DELETE FROM queue")
            self.db.commit()
            return int(rows["c"])

    def queue_prune(self, valid: Iterable[str]) -> None:
        valid = set(valid)
        with self._lock:
            self.db.execute("DELETE FROM queue")
            for key in valid:
                self.db.execute(
                    "INSERT INTO queue (track_key, added_at) VALUES (?,?)", (key, now_int()))
            self.db.commit()

    # --------------------------------------------------------------- intern
    def _persist(self, track: Track) -> None:
        self.db.execute(
            """INSERT INTO tracks (key, title, url, duration, uploader, state, requests,
                                   win_self, win_others, settled, added_at, last_seen,
                                   rel_path, bytes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   title=excluded.title, url=excluded.url, duration=excluded.duration,
                   uploader=excluded.uploader, state=excluded.state,
                   requests=excluded.requests, win_self=excluded.win_self,
                   win_others=excluded.win_others, settled=excluded.settled,
                   last_seen=excluded.last_seen, rel_path=excluded.rel_path,
                   bytes=excluded.bytes""",
            (track.key, track.title, track.url, track.duration, track.uploader,
             track.state, track.requests, track.win_self, track.win_others,
             int(track.settled), track.added_at, track.last_seen, track.rel_path,
             track.bytes),
        )

    def close(self) -> None:
        with self._lock:
            self.db.close()
