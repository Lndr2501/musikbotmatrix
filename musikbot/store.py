"""SQLite-Speicher für Titel-Metadaten und Warteschlange.

Musikdateien werden NIE aufbewahrt: Jede Datei liegt nur während der Wiedergabe
in ``tmp_dir`` und wird danach gelöscht. Jede Wiedergabe lädt neu herunter.
In der Datenbank stehen nur Titel, URL und Länge.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable

from .util import now_int

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    duration    REAL NOT NULL DEFAULT 0,
    uploader    TEXT NOT NULL DEFAULT '',
    added_at    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queue (
    pos        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key  TEXT NOT NULL,
    requester  TEXT NOT NULL DEFAULT '',
    added_at   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    owner      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL,
    track_key   TEXT NOT NULL,
    added_at    INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Track:
    key: str
    title: str = ""
    url: str = ""
    duration: float = 0.0
    uploader: str = ""
    added_at: int = 0
    abs_path: str = ""  # nur gesetzt, solange die Datei für die Wiedergabe existiert

    @property
    def playable(self) -> bool:
        return bool(self.abs_path) and os.path.exists(self.abs_path)


class TrackStore:
    def __init__(self, db_path: str, tmp_dir: str, *legacy_dirs: str) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.tmp_dir = tmp_dir
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._tracks: dict[str, Track] = {}
        # Alles Gespeicherte aus früheren Versionen (cache/, library/) und
        # Reste eines Absturzes wegräumen.
        for directory in (tmp_dir, *legacy_dirs):
            shutil.rmtree(directory, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)
        self._load()

    def _load(self) -> None:
        with self._lock:
            self._tracks.clear()
            for row in self.db.execute("SELECT key,title,url,duration,uploader,added_at FROM tracks"):
                self._tracks[row["key"]] = Track(
                    key=row["key"], title=row["title"], url=row["url"],
                    duration=row["duration"], uploader=row["uploader"],
                    added_at=row["added_at"])

    # ------------------------------------------------------- Metadaten
    def note_request(self, key: str, *, title: str = "", url: str = "",
                     duration: float = 0.0, uploader: str = "") -> Track:
        with self._lock:
            track = self._tracks.get(key)
            if track is None:
                track = Track(key=key, added_at=now_int())
                self._tracks[key] = track
            if title:
                track.title = title
            if url:
                track.url = url
            if duration:
                track.duration = duration
            if uploader:
                track.uploader = uploader
            self.db.execute(
                """INSERT INTO tracks (key,title,url,duration,uploader,added_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET title=excluded.title, url=excluded.url,
                       duration=excluded.duration, uploader=excluded.uploader""",
                (track.key, track.title, track.url, track.duration, track.uploader,
                 track.added_at))
            self.db.commit()
            return track

    # ------------------------------------------------------- Dateien
    def set_file(self, key: str, path: str) -> None:
        with self._lock:
            self._tracks[key].abs_path = path

    def release_file(self, key: str) -> None:
        """Datei des Titels löschen (nach der Wiedergabe oder bei Abbruch)."""
        with self._lock:
            track = self._tracks.get(key)
            if track is None or not track.abs_path:
                return
            path, track.abs_path = track.abs_path, ""
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        # yt-dlp-Nebendateien (.part, .ytdl, ...) mit gleichem Stamm
        stem = os.path.splitext(os.path.basename(path))[0]
        for name in os.listdir(self.tmp_dir):
            if name.startswith(stem):
                try:
                    os.remove(os.path.join(self.tmp_dir, name))
                except OSError:
                    pass

    def release_all(self) -> None:
        for key in list(self._tracks):
            self.release_file(key)

    # ------------------------------------------------------------ Abfragen
    def get(self, key: str) -> Track | None:
        with self._lock:
            return self._tracks.get(key)

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

    # ------------------------------------------------------------ Playlists
    # Eine Playlist ist nur eine geordnete Liste von Titel-Schluesseln; die
    # Metadaten (Titel, URL) stehen wie immer in ``tracks``. Dateien gibt es nie.
    def _playlist_id(self, name: str) -> int | None:
        row = self.db.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()
        return None if row is None else int(row["id"])

    def playlist_create(self, name: str, owner: str = "") -> bool:
        with self._lock:
            if self._playlist_id(name) is not None:
                return False
            self.db.execute("INSERT INTO playlists (name, owner, created_at) VALUES (?,?,?)",
                            (name, owner, now_int()))
            self.db.commit()
            return True

    def playlist_delete(self, name: str) -> bool:
        with self._lock:
            pid = self._playlist_id(name)
            if pid is None:
                return False
            self.db.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
            self.db.execute("DELETE FROM playlists WHERE id = ?", (pid,))
            self.db.commit()
            return True

    def playlist_list(self) -> list[tuple[str, int]]:
        """[(Name, Anzahl Titel)] alphabetisch."""
        with self._lock:
            rows = self.db.execute(
                """SELECT p.name AS name, COUNT(i.id) AS n FROM playlists p
                   LEFT JOIN playlist_items i ON i.playlist_id = p.id
                   GROUP BY p.id ORDER BY p.name COLLATE NOCASE""").fetchall()
            return [(r["name"], int(r["n"])) for r in rows]

    def playlist_keys(self, name: str) -> list[str] | None:
        """Titel-Schluessel in Reihenfolge; None = Playlist gibt es nicht."""
        with self._lock:
            pid = self._playlist_id(name)
            if pid is None:
                return None
            rows = self.db.execute(
                "SELECT track_key FROM playlist_items WHERE playlist_id = ? ORDER BY id ASC",
                (pid,)).fetchall()
            return [r["track_key"] for r in rows]

    def playlist_add(self, name: str, key: str) -> bool:
        with self._lock:
            pid = self._playlist_id(name)
            if pid is None:
                return False
            self.db.execute(
                "INSERT INTO playlist_items (playlist_id, track_key, added_at) VALUES (?,?,?)",
                (pid, key, now_int()))
            self.db.commit()
            return True

    def playlist_remove(self, name: str, index: int) -> str | None:
        """Entfernt den index-ten Titel (1-basiert); gibt dessen Schluessel zurueck."""
        with self._lock:
            pid = self._playlist_id(name)
            if pid is None or index < 1:
                return None
            row = self.db.execute(
                "SELECT id, track_key FROM playlist_items WHERE playlist_id = ? "
                "ORDER BY id ASC LIMIT 1 OFFSET ?", (pid, index - 1)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM playlist_items WHERE id = ?", (row["id"],))
            self.db.commit()
            return row["track_key"]

    def close(self) -> None:
        with self._lock:
            self.db.close()
