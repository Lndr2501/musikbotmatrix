"""Bot-Ablauf mit Karten: Suchen -> Laden -> Spielt in EINER Karte (Bearbeitungen), Pause/Weiter,
Fehler, Ende. Mit Attrappen fuer Matrix und Player (kein Netz, kein Audio)."""
import asyncio
from types import SimpleNamespace

from musikbot import cards
from musikbot.bot import Bot


class FakeMatrix:
    def __init__(self):
        self.events = []          # (art, event_id, content)
        self._n = 0

    async def send_card(self, room, content):
        self._n += 1
        eid = f"$e{self._n}"
        self.events.append(("send", eid, content))
        return eid

    async def edit_card(self, room, eid, content):
        self.events.append(("edit", eid, cards.edit_content(eid, content)))
        return True

    async def send_notice(self, room, text):
        self.events.append(("notice", None, {"body": text}))


def make_bot():
    bot = Bot.__new__(Bot)
    bot.matrix = FakeMatrix()
    bot._card_pending = {}
    bot._card_now = {}
    bot._fetch_room = "!r:x"
    bot.ecall = SimpleNamespace(room_id=None)
    bot.calls = SimpleNamespace(active_room=None)
    bot.player = SimpleNamespace(position=12.0, queue=[], status=lambda: {})
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
