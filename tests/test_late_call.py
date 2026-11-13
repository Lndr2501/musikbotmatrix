"""Play vor dem Call: Bot merkt sich den Raum und tritt bei, sobald jemand im Call auftaucht."""
import asyncio
from types import SimpleNamespace

from musikbot.bot import Bot
from musikbot.elementcall import ElementCall, member_state_key


class FakeEcall:
    def __init__(self):
        self.room_id = None
        self.others = {}
        self.joined = []

    async def load_members(self, room):
        pass

    def others_in_call(self, room):
        return self.others

    async def join(self, room):
        self.joined.append(room)
        self.room_id = room


def _bot(state="playing"):
    bot = Bot.__new__(Bot)
    bot.ecall = FakeEcall()
    bot._want_call = None
    bot._fetching = 0
    bot.player = SimpleNamespace(state=state, _lock=asyncio.Lock())
    bot.notices = []

    async def notice(room, text):
        bot.notices.append(text)
    bot.matrix = SimpleNamespace(send_notice=notice)
    return bot


def test_no_call_is_remembered_then_joined_when_someone_arrives():
    bot = _bot()
    assert asyncio.run(bot._join_element_call("!r", force=False)) is False
    assert bot._want_call == "!r" and bot.notices
    bot.ecall.others = {("@u:x", "D"): {}}
    asyncio.run(bot._on_call_started("!r"))
    assert bot.ecall.joined == ["!r"] and bot._want_call is None


def test_late_call_ignored_when_nothing_plays_anymore():
    bot = _bot(state="stopped")
    bot._want_call = "!r"
    asyncio.run(bot._on_call_started("!r"))
    assert bot.ecall.joined == [] and bot._want_call is None


def test_member_event_triggers_callback_only_when_outside_call():
    class Cfg:
        matrix_user_id, user, device_id = "@bot:x", "@bot:x", "BOT"
    ec = ElementCall.__new__(ElementCall)
    ec.cfg, ec.members, ec.room_id, ec._key, ec._keyed = Cfg, {}, None, None, set()
    ec.keep_alive = None
    seen = []

    async def cb(room):
        seen.append(room)
    ec.on_call_started = cb

    async def run():
        await ec.on_member_event("!r", "@u:x", member_state_key("@u:x", "D"),
                                 {"device_id": "D", "application": "m.call"})
        await asyncio.sleep(0)
    asyncio.run(run())
    assert seen == ["!r"]
