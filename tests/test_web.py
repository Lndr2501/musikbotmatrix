"""Dashboard: Login/Sitzungen, API-Schutz, Warteschlangen-Aktionen, Ausblenden."""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from musikbot.webauth import WebAuth

pytest.importorskip("aiohttp")


def _auth():
    return WebAuth(sqlite3.connect(":memory:", check_same_thread=False), threading.RLock())


def test_link_login_session_and_new_link_replaces_old():
    auth = _auth()
    link = auth.issue_link_token("@A:x", "!room:x")
    assert len(link) >= 30 and auth.room_of("@a:x") == "!room:x"
    assert auth.login_link("falsch") is None
    user, token = auth.login_link(link, "1.2.3.4")
    assert user == "@a:x" and auth.session_user(token) == "@a:x"
    assert auth.login_link(link)                     # gleicher Link geht auf mehreren Geraeten
    link2 = auth.issue_link_token("@a:x")
    assert auth.login_link(link) is None            # alter Link gilt nicht mehr
    assert auth.login_link(link2)
    assert auth.session_user(token) == "@a:x"       # bestehende Sitzung bleibt
    auth.logout(token)
    assert auth.session_user(token) is None


def test_session_is_extended_on_use():
    import time
    auth = _auth()
    _, token = auth.login_link(auth.issue_link_token("@a:x"))
    auth.db.execute("UPDATE web_sessions SET expires_at = ?", (int(time.time()) + 3600,))
    assert auth.session_user(token) == "@a:x"
    left = auth.db.execute("SELECT expires_at FROM web_sessions").fetchone()[0] - time.time()
    assert left > 80 * 86400


def test_invalid_links_are_throttled_per_ip():
    auth = _auth()
    link = auth.issue_link_token("@a:x")
    for _ in range(8):
        assert auth.login_link("nope", "9.9.9.9") is None
    assert auth.login_link(link, "9.9.9.9") is None    # gesperrt, auch mit gueltigem Link
    assert auth.login_link(link, "8.8.8.8")            # andere IP geht


class StubBot:
    def __init__(self):
        self.cfg = SimpleNamespace(matrix_user_id="@bot:m.x", user="@bot:m.x", web_host="127.0.0.1",
                                   web_port=0)
        self.webauth = _auth()
        self.calls = []
        self.resolver = SimpleNamespace(search_async=self._search)

    async def _search(self, q):
        return [{"title": q, "url": "https://x"}]

    def _allowed(self, user):
        return user != "@gesperrt:m.x"

    def web_state(self, user):
        return {"me": user, "state": "stopped", "queue": []}

    async def web_command(self, user, name, arg):
        self.calls.append((user, name, arg))


def _run(coro):
    return asyncio.run(coro)


def test_api_requires_login_header_and_session():
    from aiohttp.test_utils import TestClient, TestServer

    from musikbot.web import Dashboard

    async def go():
        bot = StubBot()
        link = bot.webauth.issue_link_token("@lndr:m.x")
        client = TestClient(TestServer(Dashboard(bot).app))
        await client.start_server()
        try:
            h = {"X-Musikbot": "1"}
            r = await client.get("/api/state")
            assert r.status == 401
            r = await client.post("/api/link", json={"token": link})
            assert r.status == 403                         # ohne Schutz-Header
            r = await client.post("/api/link", json={"token": "x"}, headers=h)
            assert r.status == 401
            r = await client.post("/api/link", json={"token": link}, headers=h)
            assert r.status == 200
            assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
            r = await client.get("/api/state")
            assert (await r.json())["me"] == "@lndr:m.x"
            r = await client.post("/api/cmd", json={"name": "skip"}, headers=h)
            assert r.status == 200 and bot.calls == [("@lndr:m.x", "skip", "")]
            r = await client.get("/api/search?q=abc")
            assert (await r.json())["results"][0]["title"] == "abc"
            r = await client.get("/")
            assert r.status == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
            assert "Musikbot" in await r.text()
            r = await client.get("/static/app.js")
            assert r.status == 200
            await client.post("/api/logout", json={}, headers=h)
            assert (await client.get("/api/state")).status == 401
        finally:
            await client.close()

    _run(go())


def test_web_queue_operations():
    from musikbot.bot import Bot
    from musikbot.ytdl import MediaError

    bot = Bot.__new__(Bot)
    bot.player = SimpleNamespace(queue=[("a", ""), ("b", ""), ("c", ""), ("d", "")])
    bot.web_queue("@u:x", "top", 2, -1)
    assert [k for k, _ in bot.player.queue] == ["c", "a", "b", "d"]
    bot.web_queue("@u:x", "move", 0, 3)
    assert [k for k, _ in bot.player.queue] == ["a", "b", "d", "c"]
    bot.web_queue("@u:x", "remove", 1, -1)
    assert [k for k, _ in bot.player.queue] == ["a", "d", "c"]
    with pytest.raises(MediaError):
        bot.web_queue("@u:x", "remove", 9, -1)


def test_master_fade_out_ramps_to_silence():
    from musikbot.config import Config
    from musikbot.hub import Hub
    from musikbot.player import Player

    cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir="/tmp/x")
    p = Player.__new__(Player)
    p.cfg = cfg
    p._master, p._master_target, p._master_rate = 1.0, 1.0, 0.0
    block = np.full((1, 1920), 10000, dtype=np.int16)
    assert p._apply_master(block) is block              # voll: nichts zu tun
    p._ramp(0.0, 0.04)                                  # 40 ms = 2 Bloecke
    first, second, third = (p._apply_master(block) for _ in range(3))
    assert first[0, 0] == 10000 and 4000 < first[0, -1] < 6000
    assert abs(int(second[0, -1])) < 300
    assert not third.any()
    p.reset_master()
    assert p._apply_master(block) is block
    assert Hub  # Import geprueft


def _bot_with_store(tmp_path):
    from musikbot.bot import Bot
    from musikbot.store import TrackStore

    bot = Bot.__new__(Bot)
    bot.store = TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp"))
    bot.player = SimpleNamespace(current=None, queue=[])
    bot._thumb_url = {}
    bot._names = {}
    bot._bg_tasks = set()
    for key in ("a", "b"):
        bot.store.note_request(key, title=key.upper())
    return bot


def test_own_playlists_only_owner_may_change(tmp_path):
    from musikbot.ytdl import MediaError

    bot = _bot_with_store(tmp_path)
    run = lambda *a, **k: asyncio.run(bot.web_playlist(*a, **k))  # noqa: E731
    run("@lndr:x", "create", "Chill")
    with pytest.raises(MediaError):
        run("@anna:x", "create", "chill")                 # Name schon vergeben
    run("@lndr:x", "add", "Chill", key="a")
    run("@lndr:x", "add", "Chill", key="b")
    assert bot.store.playlist_keys("Chill") == ["a", "b"]
    with pytest.raises(MediaError):
        run("@anna:x", "add", "Chill", key="a")           # fremde Playlist
    with pytest.raises(MediaError):
        run("@anna:x", "delete", "Chill")
    run("@lndr:x", "remove", "Chill", index=0)
    assert bot.store.playlist_keys("Chill") == ["b"]
    full = bot.store.playlists_full()
    assert full == [{"name": "Chill", "owner": "@lndr:x", "count": 1}]
    run("@lndr:x", "delete", "Chill")
    assert bot.store.playlists_full() == []


def test_personal_stats_are_per_user(tmp_path):
    bot = _bot_with_store(tmp_path)
    st = bot.store
    st.play_log("a", "@lndr:x", 100)
    st.play_log("a", "@lndr:x", 100)
    st.play_log("b", "@lndr:x", 50)
    st.play_log("b", "@anna:x", 50)
    assert st.top_of("@LNDR:x") == [("a", 2), ("b", 1)]
    assert st.recent_of("@lndr:x") == ["b", "a"]
    assert st.totals_of("@lndr:x") == {"plays": 3, "seconds": 250.0}
    assert st.top_of("@anna:x") == [("b", 1)]


def test_quick_returns_fast_but_reports_quick_errors():
    from musikbot.bot import Bot
    from musikbot.ytdl import MediaError

    bot = Bot.__new__(Bot)
    bot._bg_tasks = set()

    async def slow():
        await asyncio.sleep(1.0)

    async def broken():
        raise MediaError("kaputt")

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await bot._quick(slow(), wait=0.05)
        assert loop.time() - t0 < 0.5 and len(bot._bg_tasks) == 1   # laeuft im Hintergrund weiter
        with pytest.raises(MediaError):
            await bot._quick(broken())

    asyncio.run(go())
