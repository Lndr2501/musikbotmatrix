"""Leerer Call: Bot geht und stoppt die Musik, wenn niemand mehr zuhoert."""
import asyncio
from types import SimpleNamespace

from musikbot.bot import EMPTY_GRACE_SECONDS, Bot


class FakeEcall:
    def __init__(self, others):
        self.room_id = "!r"
        self.others = others            # LiveKit-Teilnehmer (None = nicht verbunden)
        self.left = False

    @property
    def active(self):
        return self.room_id is not None

    def livekit_others(self):
        return self.others

    async def load_members(self, room):
        pass

    def others_in_call(self, room):
        return {}

    async def leave(self):
        self.left = True
        self.room_id = None


def _bot(others, state="playing"):
    bot = Bot.__new__(Bot)
    bot.ecall = FakeEcall(others)
    bot.calls = SimpleNamespace(peers=[], active_room=None, hangups=[])

    async def hangup_all(reason):
        bot.calls.hangups.append(reason)
    bot.calls.hangup_all = hangup_all
    bot.player = SimpleNamespace(state=state)

    async def stop():
        bot.player.state = "stopped"
    bot.player.stop = stop
    bot._always = None
    bot._fetch_room = ""
    bot.notes = []

    async def note(room, text, *a):
        bot.notes.append(text)

    async def finish(room, *a):
        pass
    bot._note, bot._finish_now_card = note, finish
    return bot


def _steps(bot, times):
    return [asyncio.run(bot._check_empty(t)) for t in times]


def test_leaves_and_stops_after_grace_when_nobody_is_connected():
    bot = _bot(others=0)
    assert _steps(bot, [0, EMPTY_GRACE_SECONDS - 1, EMPTY_GRACE_SECONDS + 1]) == [False, False, True]
    assert bot.ecall.left and bot.player.state == "stopped" and bot.calls.hangups == ["empty"]
    assert "Musik gestoppt" in bot.notes[0]


def test_stays_while_someone_is_connected_and_resets_the_timer():
    bot = _bot(others=0)
    _steps(bot, [0])
    bot.ecall.others = 1                         # jemand kommt zurueck
    assert _steps(bot, [EMPTY_GRACE_SECONDS + 5]) == [False]
    bot.ecall.others = 0
    assert _steps(bot, [100, 110]) == [False, False]   # Frist beginnt neu
    assert not bot.ecall.left


def test_24_7_stays_even_when_empty():
    bot = _bot(others=0)
    bot._always = {"kind": "radio"}
    assert _steps(bot, [0, 100, 200]) == [False, False, False]
    assert not bot.ecall.left


def test_music_without_any_call_is_stopped():
    bot = _bot(others=None)
    bot.ecall.room_id = None                     # in keinem Call, spielt aber
    assert _steps(bot, [0, EMPTY_GRACE_SECONDS + 1]) == [False, True]
    assert bot.player.state == "stopped"


def test_idle_bot_outside_a_call_does_nothing():
    bot = _bot(others=None, state="stopped")
    bot.ecall.room_id = None
    assert _steps(bot, [0, 100]) == [False, False]
    assert bot.notes == []
