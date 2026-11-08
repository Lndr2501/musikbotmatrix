"""Bot-Ablauf mit Karten: Suchen -> Laden -> Spielt in EINER Karte (Bearbeitungen), Pause/Weiter,
Fehler, Ende. Mit Attrappen fuer Matrix und Player (kein Netz, kein Audio)."""
import asyncio
from types import SimpleNamespace

from musikbot import cards
from musikbot.bot import Bot


class FakeMatrix:
    def __init__(self):
        self.events = []          # (art, event_id, content)
        self.uploads = []
        self._n = 0

    async def send_card(self, room, content):
        self._n += 1
        eid = f"$e{self._n}"
        self.events.append(("send", eid, content))
        return eid

    async def edit_card(self, room, eid, content):
        self.events.append(("edit", eid, cards.edit_content(eid, content)))
        return True

    async def upload_image(self, data, content_type, name):
        self.uploads.append((len(data), content_type, name))
        return f"mxc://x.de/{name}"

    async def send_notice(self, room, text):
        self.events.append(("notice", None, {"body": text}))


class FakePlayer:
    """Nur das, was die Befehle brauchen."""

    def __init__(self):
        self.position = 12.0
        self.volume = 60
        self.state = "playing"
        self.queue = []
        self.seeks = []
        self.covers = []

    def set_cover(self, key, frame):
        self.covers.append((key, frame))

    def status(self):
        return {"duration": 197.0, "state": self.state, "position": self.position, "track": None,
                "volume": self.volume, "video": False, "queue": [], "peers": 0}

    def set_volume(self, value):
        self.volume = max(0, min(300, value))
        return self.volume

    async def skip(self):
        return False          # keine weiteren Titel

    async def seek(self, seconds):
        self.seeks.append(seconds)
        self.position = seconds
        return True


def make_bot():
    bot = Bot.__new__(Bot)
    bot.matrix = FakeMatrix()
    bot._card_pending = {}
    bot._card_now = {}
    bot._thumb_url = {}
    bot._thumb_mxc = {}
    bot._cover_frames = {}
    bot._cmds_done = set()
    bot._thumb_tasks = {}
    bot._loop_on = False
    bot._always = None
    bot._fetch_room = "!r:x"
    bot.ecall = SimpleNamespace(room_id=None)
    bot.calls = SimpleNamespace(active_room=None)
    bot.player = FakePlayer()
    return bot


def track(title="Cheri Cheri Lady", key="k1", duration=197.0, uploader="Modern Talking"):
    return SimpleNamespace(title=title, key=key, duration=duration, uploader=uploader, url="https://x")


def card_of(event):
    return event[2][cards.CARD_KEY] if event[0] == "send" else event[2]["m.new_content"][cards.CARD_KEY]


def test_search_download_playing_is_one_card_edited_twice():
    bot = make_bot()
    t = track()

    async def flow():
        sc = cards.track_card("searching", "cherry")
        eid = await bot.matrix.send_card("!r:x", cards.card_content(cards.track_body(sc), sc))
        bot._card_pending["!r:x"] = eid
        # Download (so wie _fetch es macht)
        dl = cards.track_card("downloading", t.title, **bot._track_meta(t))
        e2 = await bot._put_track_card("!r:x", dl, bot._card_pending.get("!r:x"))
        bot._card_pending["!r:x"] = e2
        await bot._announce(t)

    asyncio.run(flow())
    ev = bot.matrix.events
    assert [e[0] for e in ev] == ["send", "edit", "edit"]
    assert {e[1] for e in ev} == {"$e1"}, "alles dieselbe Nachricht"
    assert [card_of(e)["state"] for e in ev] == ["searching", "downloading", "playing"]
    last = card_of(ev[-1])
    assert last["position"] == 0.0 and last["startedAt"] > 0
    assert last["actions"][0]["send"] == "!pause"
    assert bot._card_now["!r:x"][0] == "$e1"
    assert "!r:x" not in bot._card_pending


def test_next_track_finishes_the_old_card_and_sends_a_new_one():
    bot = make_bot()

    async def flow():
        await bot._announce(track("Erster", "a"))
        await bot._announce(track("Zweiter", "b"))

    asyncio.run(flow())
    ev = bot.matrix.events
    assert [e[0] for e in ev] == ["send", "edit", "send"]
    assert card_of(ev[1])["state"] == "stopped" and "actions" not in card_of(ev[1])
    assert card_of(ev[2])["title"] == "Zweiter"
    assert bot._card_now["!r:x"][0] == "$e2"


def test_pause_and_resume_edit_the_playing_card():
    bot = make_bot()

    async def flow():
        await bot._announce(track())
        ok1 = await bot._now_card_state("!r:x", "paused")
        ok2 = await bot._now_card_state("!r:x", "playing")
        none = await bot._now_card_state("!other", "paused")
        return ok1, ok2, none

    ok1, ok2, none = asyncio.run(flow())
    assert ok1 and ok2 and not none
    states = [card_of(e)["state"] for e in bot.matrix.events]
    assert states == ["playing", "paused", "playing"]
    paused = card_of(bot.matrix.events[1])
    assert paused["position"] == 12.0 and "startedAt" not in paused
    assert paused["actions"][0]["send"] == "!resume"


def test_failure_turns_the_pending_card_into_an_error_card():
    bot = make_bot()

    async def flow():
        sc = cards.track_card("searching", "cherry")
        bot._card_pending["!r:x"] = await bot.matrix.send_card(
            "!r:x", cards.card_content(cards.track_body(sc), sc))
        await bot._fail_pending("!r:x", "cherry", "Nichts gefunden")

    asyncio.run(flow())
    ev = bot.matrix.events
    assert [e[0] for e in ev] == ["send", "edit"]
    err = card_of(ev[1])
    assert err["state"] == "error" and err["note"] == "Nichts gefunden"
    assert bot._card_pending == {}


def test_finish_now_card_sets_stopped_and_forgets_it():
    bot = make_bot()

    async def flow():
        await bot._announce(track())
        await bot._finish_now_card("!r:x")
        await bot._finish_now_card("!r:x")      # zweimal ist harmlos

    asyncio.run(flow())
    assert [card_of(e)["state"] for e in bot.matrix.events] == ["playing", "stopped"]
    assert bot._card_now == {}


def run_cmd(bot, name, arg=""):
    asyncio.run(bot._dispatch("!r:x", "@u:x", name, arg))


def test_volume_buttons_change_the_volume_relatively_and_update_the_card():
    bot = make_bot()
    asyncio.run(bot._announce(track()))
    run_cmd(bot, "volume", "+10")
    run_cmd(bot, "volume", "-30")
    assert bot.player.volume == 40
    ev = bot.matrix.events
    assert [e[0] for e in ev] == ["send", "edit", "edit"], "keine Chips, nur Bearbeitungen der Karte"
    assert [card_of(e)["volume"] for e in ev] == [60, 70, 40]
    assert card_of(ev[-1])["state"] == "playing" and card_of(ev[-1])["seek"] == "!seek"


def test_absolute_volume_and_limits():
    bot = make_bot()
    asyncio.run(bot._announce(track()))
    run_cmd(bot, "volume", "80")
    run_cmd(bot, "volume", "+500")
    assert bot.player.volume == 300
    run_cmd(bot, "volume", "-1000")
    assert bot.player.volume == 0


def test_volume_without_a_card_falls_back_to_a_note_and_bad_input_shows_usage():
    bot = make_bot()
    run_cmd(bot, "volume", "+10")
    ev = bot.matrix.events
    assert ev[-1][0] == "send" and ev[-1][2]["body"] == "🔊 Lautstärke: 70"
    run_cmd(bot, "volume", "laut")
    assert "Benutzung" in bot.matrix.events[-1][2]["body"]
    assert bot.player.volume == 70


def test_seek_updates_the_card_with_the_new_position():
    bot = make_bot()
    asyncio.run(bot._announce(track()))
    run_cmd(bot, "seek", "1:30")
    assert bot.player.seeks == [90.0]
    last = bot.matrix.events[-1]
    assert last[0] == "edit"
    c = card_of(last)
    assert c["state"] == "playing" and c["position"] == 90.0 and c["startedAt"] > 0


def test_seek_while_paused_keeps_the_card_paused():
    bot = make_bot()
    asyncio.run(bot._announce(track()))
    bot.player.state = "paused"
    run_cmd(bot, "forward", "10")
    c = card_of(bot.matrix.events[-1])
    assert c["state"] == "paused" and "startedAt" not in c


def test_thumbnail_source_prefers_ytdlp_then_derives_youtube():
    bot = make_bot()
    assert bot._thumb_source("youtube:9q8M_T8PC2Q") == "https://i.ytimg.com/vi/9q8M_T8PC2Q/mqdefault.jpg"
    bot._thumb_url["youtube:abcdefghijk"] = "https://example.org/cover.jpg"
    assert bot._thumb_source("youtube:abcdefghijk") == "https://example.org/cover.jpg"
    assert bot._thumb_source("soundcloud:123") == ""
    assert bot._thumb_source("youtube:../../etc") == "", "keine Pfad-Tricks in der Video-ID"


def test_cover_is_downloaded_uploaded_once_and_put_into_the_card():
    from aiohttp import web

    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")

    async def flow():
        async def image(_):
            return web.Response(body=png, content_type="image/png")

        async def page(_):
            return web.Response(text="<html>kein Bild</html>", content_type="text/html")

        app = web.Application()
        app.router.add_get("/c.png", image)
        app.router.add_get("/c.html", page)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        bot = make_bot()
        bot._thumb_url["a:1"] = f"http://127.0.0.1:{port}/c.png"
        bot._thumb_url["a:2"] = f"http://127.0.0.1:{port}/c.html"
        try:
            first = await bot._fetch_thumb("a:1")
            again = await bot._fetch_thumb("a:1")
            not_image = await bot._fetch_thumb("a:2")
            await bot._announce(track(key="a:1"))
        finally:
            await runner.cleanup()
        return bot, first, again, not_image

    bot, first, again, not_image = asyncio.run(flow())
    assert first == again == "mxc://x.de/cover.png"
    assert len(bot.matrix.uploads) == 1, "ein Titel wird nur einmal hochgeladen"
    assert not_image == "", "eine Webseite ist kein Titelbild"
    c = card_of(bot.matrix.events[-1])
    assert c["thumbnail"] == "mxc://x.de/cover.png"


def test_pause_keeps_the_cover_on_the_card():
    bot = make_bot()
    bot._thumb_mxc["k1"] = "mxc://x.de/cover.jpg"

    async def flow():
        await bot._announce(track())
        await bot._now_card_state("!r:x", "paused")

    asyncio.run(flow())
    assert all(card_of(e)["thumbnail"] == "mxc://x.de/cover.jpg" for e in bot.matrix.events)


def test_skip_without_a_next_track_ends_the_card_and_says_so():
    bot = make_bot()
    asyncio.run(bot._announce(track()))
    run_cmd(bot, "skip", "")
    track_cards = [card_of(e) for e in bot.matrix.events if cards.CARD_KEY in (
        e[2] if e[0] == "send" else e[2].get("m.new_content", {}))]
    states = [c["state"] for c in track_cards if c["kind"] == "track"]
    assert states[:2] == ["playing", "stopped"]
    assert "Keine weiteren Titel" in bot.matrix.events[-1][2]["body"]
    assert bot._card_now == {}
