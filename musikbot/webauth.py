"""Dashboard-Login: persoenlicher Login-Link (per DM vom Bot), Sitzungen, Bremse.

`!dashboard` erzeugt einen Link mit geheimem Schluessel im #-Teil der Adresse
(der geht nie an Server-Logs oder andere Seiten). Wer ihn oeffnet, bekommt eine
Sitzung, die sich bei jeder Nutzung verlaengert – man bleibt also eingeloggt.
Ein neues `!dashboard` macht den alten Link ungueltig; Sitzungen bleiben.
Schluessel und Sitzungs-Tokens liegen nur als Hash in der Datenbank.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque

SESSION_DAYS = 90       # verlaengert sich bei jeder Nutzung
MAX_FAILS = 8            # ungueltige Links ...
FAIL_WINDOW = 600.0      # ... pro 10 Minuten und IP

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_users (
    user_id    TEXT PRIMARY KEY,
    pw_hash    TEXT NOT NULL,   -- "link$" + sha256 des Link-Schluessels
    room_id    TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
"""

def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class WebAuth:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock | threading.Lock) -> None:
        self.db = db
        self._lock = lock
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        self._fails: dict[str, deque] = defaultdict(deque)

    def room_of(self, user_id: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT room_id FROM web_users WHERE user_id = ?",
                                  (user_id.lower(),)).fetchone()
        return row[0] if row else ""

    # --------------------------------------------------------------- Bremse
    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        for key in keys:
            q = self._fails[key]
            while q and now - q[0] > FAIL_WINDOW:
                q.popleft()
            if len(q) >= MAX_FAILS:
                return True
        return False

    def _fail(self, *keys: str) -> None:
        now = time.monotonic()
        for key in keys:
            self._fails[key].append(now)

    # ---------------------------------------------------------- Login-Link
    def issue_link_token(self, user_id: str, room_id: str = "") -> str:
        """Neuer Link-Schluessel fuer `user_id` (ersetzt den alten). Klartext nur fuer die DM."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.db.execute(
                """INSERT INTO web_users (user_id, pw_hash, room_id, created_at) VALUES (?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET pw_hash = excluded.pw_hash,
                       room_id = excluded.room_id, created_at = excluded.created_at""",
                (user_id.lower(), "link$" + _sha(token), room_id, int(time.time())))
            self.db.commit()
        return token

    def login_link(self, token: str, ip: str = "") -> tuple[str, str] | None:
        """Link-Schluessel -> (user_id, Sitzungs-Token) oder None."""
        key = f"ip:{ip}"
        if self.blocked(key) or not token:
            return None
        with self._lock:
            row = self.db.execute("SELECT user_id FROM web_users WHERE pw_hash = ?",
                                  ("link$" + _sha(token.strip()),)).fetchone()
        if row is None:
            self._fail(key)
            return None
        return row[0], self._new_session(row[0])

    def _new_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (int(time.time()),))
            self.db.execute("INSERT INTO web_sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
                            (_sha(token), user_id, int(time.time()) + SESSION_DAYS * 86400))
            self.db.commit()
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            row = self.db.execute("SELECT user_id, expires_at FROM web_sessions WHERE token_hash = ?",
                                  (_sha(token),)).fetchone()
        now = int(time.time())
        if row is None or row[1] < now:
            return None
        if row[1] - now < (SESSION_DAYS - 1) * 86400:     # hoechstens einmal am Tag verlaengern
            with self._lock:
                self.db.execute("UPDATE web_sessions SET expires_at = ? WHERE token_hash = ?",
                                (now + SESSION_DAYS * 86400, _sha(token)))
                self.db.commit()
        return row[0]

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self.db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_sha(token),))
            self.db.commit()
