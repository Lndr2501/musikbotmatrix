"""SQLite-Speicher für Titel-Metadaten und Warteschlange.

Musikdateien werden NIE aufbewahrt: Jede Datei liegt nur während der Wiedergabe
in ``tmp_dir`` und wird danach gelöscht. Jede Wiedergabe lädt neu herunter.
In der Datenbank stehen nur Titel, URL und Länge.
"""

from __future__ import annotations

import logging
import os
import re
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
CREATE TABLE IF NOT EXISTS favorites (
    user      TEXT NOT NULL,
    track_key TEXT NOT NULL,
    added_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user, track_key)
);
CREATE TABLE IF NOT EXISTS hot (
    track_key TEXT PRIMARY KEY,
    downloads INTEGER NOT NULL DEFAULT 0,
    last_used INTEGER NOT NULL DEFAULT 0,
    path      TEXT NOT NULL DEFAULT '',
    video     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS plays (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key TEXT NOT NULL,
    requester TEXT NOT NULL DEFAULT '',
    seconds   REAL NOT NULL DEFAULT 0,
    played_at INTEGER NOT NULL DEFAULT 0
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
    def __init__(self, db_path: str, tmp_dir: str, *legacy_dirs: str,
                 hot_dir: str = "", hot_after: int = 5, hot_days: float = 3.0,
                 keep_dirs: tuple[str, ...] = ()) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.tmp_dir = tmp_dir
        # Eigene Ordner (z. B. Radio): Dateien darin werden nie geloescht.
        self.keep_dirs = tuple(os.path.abspath(d) for d in keep_dirs if d)
        self.hot_dir = hot_dir if hot_after > 0 else ""
        self.hot_after = hot_after
        self.hot_seconds = int(hot_days * 86400)
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
        if self.hot_dir:
            os.makedirs(self.hot_dir, exist_ok=True)
        self._load()
        self.prune_hot()

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

    # ------------------------------------------------- oft gespielt: behalten
    def _in_hot(self, path: str) -> bool:
        return bool(self.hot_dir) and os.path.dirname(os.path.abspath(path)) == os.path.abspath(self.hot_dir)

    def _in_keep(self, path: str) -> bool:
        full = os.path.abspath(path)
        return any(full.startswith(d + os.sep) for d in self.keep_dirs)

    def hot_path(self, key: str, want_video: bool = False) -> str | None:
        """Gemerkte Datei des Titels (und Nutzung vermerken), sonst None. Ohne Video reicht jede Datei."""
        if not self.hot_dir:
            return None
        with self._lock:
            row = self.db.execute("SELECT path, video FROM hot WHERE track_key = ?", (key,)).fetchone()
            if row is None or not row["path"] or not os.path.exists(row["path"]):
                return None
            if want_video and not row["video"]:
                return None
            self.db.execute("UPDATE hot SET last_used = ? WHERE track_key = ?", (now_int(), key))
            self.db.commit()
            return row["path"]

    def note_download(self, key: str, video: bool = False) -> int:
        """Zaehlt einen echten Download; gibt die bisherige Anzahl zurueck."""
        with self._lock:
            self.db.execute(
                """INSERT INTO hot (track_key, downloads, last_used, video) VALUES (?,1,?,?)
                   ON CONFLICT(track_key) DO UPDATE SET downloads = downloads + 1, last_used = excluded.last_used""",
                (key, now_int(), int(video)))
            self.db.commit()
            return int(self.db.execute("SELECT downloads FROM hot WHERE track_key = ?", (key,)).fetchone()[0])

    def _keep_hot(self, key: str, path: str) -> bool:
        """Datei nach der Wiedergabe behalten, wenn der Titel oft genug geladen wurde."""
        if not self.hot_dir or self._in_hot(path) or not os.path.exists(path):
            return False
        with self._lock:
            row = self.db.execute("SELECT downloads, video FROM hot WHERE track_key = ?", (key,)).fetchone()
            if row is None or row["downloads"] < self.hot_after:
                return False
            dest = os.path.join(self.hot_dir, re.sub(r"[^\w.-]", "_", key) + os.path.splitext(path)[1])
            try:
                shutil.move(path, dest)
            except OSError:
                log.warning("Titel %s lässt sich nicht merken", key, exc_info=True)
                return False
            self.db.execute("UPDATE hot SET path = ?, last_used = ? WHERE track_key = ?",
                            (dest, now_int(), key))
            self.db.commit()
            return True

    def prune_hot(self) -> int:
        """Gemerkte Titel loeschen, die laenger als hot_days nicht gespielt wurden. Gibt die Anzahl zurueck."""
        if not self.hot_dir:
            return 0
        n = 0
        with self._lock:
            cutoff = now_int() - self.hot_seconds
            for row in self.db.execute("SELECT track_key, path, last_used FROM hot WHERE path != ''").fetchall():
                if row["last_used"] < cutoff or not os.path.exists(row["path"]):
                    try:
                        os.remove(row["path"])
                    except FileNotFoundError:
                        pass
                    # Zaehler beginnt von vorn: ein Titel muss sich den Platz neu verdienen
                    self.db.execute("UPDATE hot SET path = '', downloads = 0 WHERE track_key = ?",
                                    (row["track_key"],))
                    n += 1
            self.db.commit()
            known = {r["path"] for r in self.db.execute("SELECT path FROM hot WHERE path != ''")}
        for name in os.listdir(self.hot_dir):       # Waisen (z. B. nach Absturz) wegraeumen
            full = os.path.join(self.hot_dir, name)
            if full not in known:
                try:
                    os.remove(full)
                except OSError:
                    pass
        return n

    def release_file(self, key: str) -> None:
        """Datei des Titels löschen (nach der Wiedergabe oder bei Abbruch)."""
        with self._lock:
            track = self._tracks.get(key)
            if track is None or not track.abs_path:
                return
            path, track.abs_path = track.abs_path, ""
        if self._in_keep(path) or self._in_hot(path) or self._keep_hot(key, path):
            return
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

    # ------------------------------------------------------------ Favoriten
    # Pro Nutzer (Matrix-ID, klein geschrieben) eine Liste von Titel-Schluesseln.
    def fav_add(self, user: str, key: str) -> bool:
        """False, wenn der Titel schon in den Favoriten ist."""
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO favorites (user, track_key, added_at) VALUES (?,?,?)",
                (user.lower(), key, now_int()))
            self.db.commit()
            return cur.rowcount > 0

    def fav_keys(self, user: str) -> list[str]:
        with self._lock:
            rows = self.db.execute(
                "SELECT track_key FROM favorites WHERE user = ? ORDER BY added_at ASC, rowid ASC",
                (user.lower(),)).fetchall()
            return [r["track_key"] for r in rows]

    def fav_remove(self, user: str, index: int) -> str | None:
        """Entfernt den index-ten Favoriten (1-basiert); gibt dessen Schluessel zurueck."""
        keys = self.fav_keys(user)
        if index < 1 or index > len(keys):
            return None
        with self._lock:
            self.db.execute("DELETE FROM favorites WHERE user = ? AND track_key = ?",
                            (user.lower(), keys[index - 1]))
            self.db.commit()
        return keys[index - 1]

    # ------------------------------------------------------------ Statistik
    def play_log(self, key: str, requester: str, seconds: float = 0.0) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO plays (track_key, requester, seconds, played_at) VALUES (?,?,?,?)",
                (key, requester.lower(), float(seconds or 0), now_int()))
            self.db.commit()

    def top_tracks(self, limit: int = 10, since: int = 0) -> list[tuple[str, int]]:
        """[(Titel-Schluessel, Anzahl Wiedergaben)], haeufigste zuerst."""
        with self._lock:
            rows = self.db.execute(
                """SELECT track_key, COUNT(*) AS n FROM plays WHERE played_at >= ?
                   GROUP BY track_key ORDER BY n DESC, MAX(id) DESC LIMIT ?""",
                (since, limit)).fetchall()
            return [(r["track_key"], int(r["n"])) for r in rows]

    def top_listeners(self, limit: int = 10, since: int = 0) -> list[tuple[str, int, float]]:
        """[(Nutzer, Anzahl Titel, Sekunden)], meiste Musik zuerst."""
        with self._lock:
            rows = self.db.execute(
                """SELECT requester, COUNT(*) AS n, SUM(seconds) AS s FROM plays
                   WHERE played_at >= ? GROUP BY requester ORDER BY s DESC, n DESC LIMIT ?""",
                (since, limit)).fetchall()
            return [(r["requester"], int(r["n"]), float(r["s"] or 0)) for r in rows]

    # ------------------------------------------------------------ Einstellungen
    def setting(self, name: str, default: str = "") -> str:
        with self._lock:
            row = self.db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row[0] if row else default

    def set_setting(self, name: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO settings (name, value) VALUES (?,?) "
                            "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))
            self.db.commit()

    # ------------------------------------------------------------ pro Nutzer (Dashboard)
    def playlists_full(self) -> list[dict]:
        """Alle Playlists mit Besitzer und Anzahl Titel."""
        with self._lock:
            rows = self.db.execute(
                """SELECT p.name AS name, p.owner AS owner, COUNT(i.id) AS n FROM playlists p
                   LEFT JOIN playlist_items i ON i.playlist_id = p.id
                   GROUP BY p.id ORDER BY p.name COLLATE NOCASE""").fetchall()
        return [{"name": r["name"], "owner": r["owner"], "count": int(r["n"])} for r in rows]

    def playlist_owner(self, name: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT owner FROM playlists WHERE name = ?", (name,)).fetchone()
        return None if row is None else row["owner"]

    def top_of(self, user: str, limit: int = 10) -> list[tuple[str, int]]:
        with self._lock:
            rows = self.db.execute(
                """SELECT track_key, COUNT(*) AS n FROM plays WHERE lower(requester) = ?
                   GROUP BY track_key ORDER BY n DESC, MAX(id) DESC LIMIT ?""",
                (user.lower(), limit)).fetchall()
        return [(r["track_key"], int(r["n"])) for r in rows]

    def recent_of(self, user: str, limit: int = 10) -> list[str]:
        with self._lock:
            rows = self.db.execute(
                """SELECT track_key FROM plays WHERE lower(requester) = ?
                   GROUP BY track_key ORDER BY MAX(id) DESC LIMIT ?""",
                (user.lower(), limit)).fetchall()
        return [r["track_key"] for r in rows]

    def totals_of(self, user: str) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(seconds), 0) AS s FROM plays WHERE lower(requester) = ?",
                (user.lower(),)).fetchone()
        return {"plays": int(row["n"]), "seconds": float(row["s"])}

    def close(self) -> None:
        with self._lock:
            self.db.close()
