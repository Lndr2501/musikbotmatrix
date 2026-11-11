"""Autoplay: aehnliche Titel, wenn die Warteschlange leer wird."""
import asyncio
from types import SimpleNamespace

from musikbot.config import Config
from musikbot.hub import Hub
from musikbot.player import AUTOPLAY, Player
from musikbot.store import TrackStore


def _player(tmp_path):
    cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir=str(tmp_path))
    return Player(cfg, Hub(), TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp")))


def test_human_wishes_go_before_autoplay_titles(tmp_path):
    p = _player(tmp_path)
    p.enqueue("auto1", AUTOPLAY)
    p.enqueue("mine", "@lndr:x")
    p.enqueue("auto2", AUTOPLAY)
    p.enqueue("yours", "@anna:x")
    assert [k for k, _ in p.queue] == ["mine", "yours", "auto1", "auto2"]


def test_need_next_fires_once_per_track_near_the_end(tmp_path):
    p = _player(tmp_path)
    events = []

    async def on_event(name, data):
        events.append((name, data["track"].key))

    async def go():
        p.on_event = on_event
        p.current = SimpleNamespace(key="k1")
        p._source = SimpleNamespace(duration=300.0)
        p._pos = 100.0                      # noch weit vom Ende
        p._maybe_need_next()
        p._pos = 300.0 - 6 - 45 - 30 + 1    # Crossfade + Vorladen + need-next
        p._maybe_need_next()
        p._maybe_need_next()                # nicht doppelt
        p.queue.append(("x", ""))
        p.current = SimpleNamespace(key="k2")
        p._maybe_need_next()                # Warteschlange nicht leer -> nichts
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert events == [("need-next", "k1")]


def _bot(tmp_path, found):
    from musikbot.bot import Bot

    bot = Bot.__new__(Bot)
    bot.cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir=str(tmp_path))
    bot.store = TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp"))
    bot.player = SimpleNamespace(queue=[], state="playing",
                                 enqueue=lambda k, r: bot.player.queue.append((k, r)))
    bot.resolver = SimpleNamespace(similar_async=lambda *a: _done(found))
    bot._autoplay, bot._autoplay_busy, bot._loop_on, bot._always = True, False, False, None
    bot._recent, bot._history, bot._thumb_url, bot._thumb_mxc, bot._thumb_tasks = [], [], {}, {}, {}
    bot._prefetch_thumb = lambda key: None
    return bot


async def _done(value):
    return value


def _cand(key, duration=200.0):
    return {"key": key, "url": f"https://y/{key}", "title": key.upper(), "artist": "A",
            "duration": duration, "thumbnail": "https://i/x.jpg"}


def test_autoplay_picks_something_new_and_queues_it(tmp_path):
    seed = SimpleNamespace(key="youtube:seed", title="Seed", uploader="A")
    found = [_cand("youtube:old"), _cand("youtube:short", 30), _cand("youtube:long", 9999),
             _cand("youtube:new")]
    bot = _bot(tmp_path, found)
    bot._recent = ["youtube:old"]                       # gerade erst gelaufen
    assert asyncio.run(bot._autoplay_pick(seed, start=False))
    assert bot.player.queue == [("youtube:new", AUTOPLAY)]
    assert bot.store.get("youtube:new").title == "YOUTUBE:NEW"


def test_autoplay_does_nothing_when_off_loop_or_someone_wished(tmp_path):
    seed = SimpleNamespace(key="youtube:seed", title="Seed", uploader="A")
    bot = _bot(tmp_path, [_cand("youtube:new")])
    bot._loop_on = True
    assert not bot._autoplay_on()
    bot._loop_on, bot._always = False, {"kind": "radio"}
    assert not bot._autoplay_on()
    bot._always = None
    bot.player.queue.append(("wunsch", "@lndr:x"))      # Warteschlange inzwischen nicht leer
    assert not asyncio.run(bot._autoplay_pick(seed, start=False))
    assert bot.player.queue == [("wunsch", "@lndr:x")]
    bot.player.queue.clear()
    bot.resolver = SimpleNamespace(similar_async=lambda *a: _done([]))
    assert not asyncio.run(bot._autoplay_pick(seed, start=False))
