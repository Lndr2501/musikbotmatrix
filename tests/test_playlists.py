"""Playlists, 24/7, Call-Pflicht und Auto-Verlassen (ohne Netzwerk)."""
import asyncio

import pytest

from musikbot.bot import Bot
from musikbot.commands import is_read_only, parse
from musikbot.config import Config
from musikbot.store import TrackStore


def test_store_playlist_roundtrip(tmp_path):
    st = TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp"))
    assert st.playlist_create("Chill", "@a:x")
    assert not st.playlist_create("chill", "@b:x")        # Name ohne Gross/Klein eindeutig
    for k in ("yt:1", "yt:2", "yt:3"):
        st.note_request(k, title=k)
        assert st.playlist_add("CHILL", k)
    assert st.playlist_keys("chill") == ["yt:1", "yt:2", "yt:3"]
    assert st.playlist_remove("chill", 2) == "yt:2"
    assert st.playlist_remove("chill", 9) is None
    assert st.playlist_keys("chill") == ["yt:1", "yt:3"]
    assert st.playlist_list() == [("Chill", 2)]
    assert st.playlist_delete("chill")
    assert st.playlist_keys("chill") is None and st.playlist_list() == []
    assert not st.playlist_add("chill", "yt:1")


def test_store_playlist_survives_reopen(tmp_path):
    db = str(tmp_path / "db")
    st = TrackStore(db, str(tmp_path / "tmp"))
    st.playlist_create("a")
    st.playlist_add("a", "yt:1")
    st.close()
    assert TrackStore(db, str(tmp_path / "tmp")).playlist_keys("a") == ["yt:1"]


def test_command_aliases_and_read_only():
    assert parse("!pl list", ("!",)).name == "playlist"
    assert parse("!24/7 song x", ("!",)).name == "always"
    assert parse("!247 off", ("!",)).name == "always"
    assert is_read_only("status") and is_read_only("queue") and is_read_only("help")
    assert is_read_only("playlist", "") and is_read_only("playlist", "list")
    assert is_read_only("playlist", "show foo")
    assert not is_read_only("playlist", "play foo")
    assert not is_read_only("playlist", "add foo http://x")
    assert is_read_only("always", "") and not is_read_only("always", "off")
    for n in ("play", "pause", "skip", "stop", "volume", "joincall", "leavecall", "clear"):
        assert not is_read_only(n)


# ---------------------------------------------------------------- Bot-Logik
@pytest.fixture
def bot(tmp_path):
    cfg = Config(homeserver="http://h", user="@bot:x", password="p",
                 data_dir=str(tmp_path), matrix_user_id="@bot:x")
    b = Bot(cfg)
    b.sent = []

    async def notice(room, text):
        b.sent.append(text)

    b.matrix.send_notice = notice

    # Diddy-Karten gehen ueber send_card/edit_card; der lesbare Rueckfall-Text (body) landet in b.sent
    async def send_card(room, content):
        b.sent.append(content["body"])
        return f"$c{len(b.sent)}"

    async def edit_card(room, event_id, content):
        b.sent.append(content["body"])
        return True

    b.matrix.send_card = send_card
    b.matrix.edit_card = edit_card
    b.played = []

    async def play(track, requester=""):
        b.player.current = track
        b.player.state = "playing"
        b.played.append(track.key)

    b.player.play = play
    b.left = []

    async def leave():
        b.left.append("ecall")

    b.ecall.leave = leave
    return b


def in_call(b, room, *users):
    b.ecall.members[room] = {
        f"_{u}_D_m.call": {"device_id": "D", "_sender": u, "application": "m.call"}
        for u in users}

    async def noload(_room):
        return None

    b.ecall.load_members = noload


def run(coro):
    return asyncio.run(coro)


def test_only_people_in_call_can_control(bot):
    in_call(bot, "!r", "@alice:x")
    run(bot._on_text("!r", "@bob:x", "!pause"))
    assert "Call sind" in bot.sent[-1]
    run(bot._on_text("!r", "@bob:x", "!status"))              # Lesen ist frei
    assert "Call sind" not in bot.sent[-1]
    run(bot._on_text("!r", "@ALICE:x", "!pause"))              # Gross/Klein egal
    assert "Call sind" not in bot.sent[-1]


def test_call_check_can_be_disabled(bot):
    bot.cfg.call_only_control = False
    in_call(bot, "!r")
    run(bot._on_text("!r", "@bob:x", "!pause"))
    assert "Call sind" not in bot.sent[-1]


def test_leaves_when_queue_empty_and_song_over(bot):
    bot.ecall.room_id = "!r"
    run(bot._player_event("queue-empty", {"auto": True}))
    assert bot.left == ["ecall"] and "verlassen" in bot.sent[-1]


def test_manual_skip_does_not_leave(bot):
    bot.ecall.room_id = "!r"
    run(bot._player_event("queue-empty", {"auto": False}))
    assert bot.left == []


def test_no_leave_while_downloading(bot):
    bot.ecall.room_id = "!r"
    bot._fetching = 1
    run(bot._player_event("queue-empty", {"auto": True}))
    assert bot.left == []


def test_playlist_play_and_24_7_refill(bot):
    in_call(bot, "!r", "@a:x")
    for k in ("yt:1", "yt:2"):
        bot.store.note_request(k, title=k, url="u")
    bot.store.playlist_create("mix")
    bot.store.playlist_add("mix", "yt:1")
    bot.store.playlist_add("mix", "yt:2")
    run(bot._on_text("!r", "@a:x", "!24/7 playlist mix"))
    assert bot.played == ["yt:1"] and bot.player.queue == [("yt:2", "@a:x")]
    # Titel 1 endet, Queue nicht leer -> kein Refill
    run(bot._player_event("track-end", {"track": bot.player.current}))
    assert len(bot.player.queue) == 1
    # letzter Titel endet -> Playlist wird neu eingereiht, Bot bleibt
    bot.player.queue.clear()
    run(bot._player_event("track-end", {"track": bot.player.current}))
    assert [k for k, _ in bot.player.queue] == ["yt:1", "yt:2"]
    bot.ecall.room_id = "!r"
    run(bot._player_event("queue-empty", {"auto": True}))
    assert bot.left == []                                      # 24/7 -> nie verlassen
    run(bot._on_text("!r", "@a:x", "!24/7 off"))
    assert bot._always is None
    run(bot._player_event("queue-empty", {"auto": True}))
    assert bot.left == ["ecall"]


def test_24_7_stays_in_empty_call(bot):
    bot._always = {"kind": "song", "keys": ["yt:1"]}
    assert bot.ecall.keep_alive() is True


def test_playlist_commands(bot):
    in_call(bot, "!r", "@a:x")
    say = lambda t: run(bot._on_text("!r", "@a:x", t))
    say("!playlist create mix")
    say("!playlist create mix")
    assert "schon" in bot.sent[-1]
    say("!playlist create bad name!")
    say("!playlist show nope")
    assert "gibt es nicht" in bot.sent[-1]
    bot.store.note_request("yt:1", title="Song Eins", url="u")
    bot.store.playlist_add("mix", "yt:1")
    say("!playlist show mix")
    assert "1. Song Eins" in bot.sent[-1]
    say("!playlist list")
    assert "mix (1 Titel)" in bot.sent[-1]
    say("!playlist remove mix 1")
    assert bot.store.playlist_keys("mix") == []
    say("!playlist play mix")
    assert "leer" in bot.sent[-1]
    say("!playlist delete mix")
    assert bot.store.playlist_keys("mix") is None


# ---------------------------------------------------------------- Favoriten und Statistik
def test_store_favorites_and_stats(tmp_path):
    st = TrackStore(str(tmp_path / "db"), str(tmp_path / "tmp"))
    assert st.fav_add("@A:x", "yt:1") and not st.fav_add("@a:x", "yt:1")   # pro Nutzer, ohne Gross/Klein
    st.fav_add("@a:x", "yt:2")
    st.fav_add("@b:x", "yt:2")
    assert st.fav_keys("@a:x") == ["yt:1", "yt:2"] and st.fav_keys("@b:x") == ["yt:2"]
    assert st.fav_remove("@a:x", 1) == "yt:1" and st.fav_remove("@a:x", 5) is None
    for k, u, s in (("yt:1", "@a:x", 60), ("yt:1", "@b:x", 60), ("yt:2", "@a:x", 180)):
        st.play_log(k, u, s)
    assert st.top_tracks(5) == [("yt:1", 2), ("yt:2", 1)]
    assert st.top_listeners(5)[0] == ("@a:x", 2, 240.0)


def test_fav_commands_and_stats_card(bot):
    in_call(bot, "!r", "@a:x")
    cards_sent = []

    async def send_card(room, content):
        cards_sent.append(content)
        bot.sent.append(content["body"])
        return "$x"

    bot.matrix.send_card = send_card
    say = lambda t, u="@a:x": run(bot._on_text("!r", u, t))
    say("!fav")
    assert "läuft nichts" in bot.sent[-1]
    bot.store.note_request("yt:1", title="Song Eins", url="u", duration=120)
    bot.player.current = bot.store.get("yt:1")
    say("!fav", "@b:x")                                        # Merken geht auch ausserhalb des Calls
    assert "Favoriten" in bot.sent[-1]
    say("!fav list", "@b:x")
    assert cards_sent[-1]["dev.diddy.card"]["kind"] == "list"
    assert cards_sent[-1]["dev.diddy.card"]["items"][0]["title"] == "Song Eins"
    say("!fav list", "@a:x")                                   # a hat nichts
    assert cards_sent[-1]["dev.diddy.card"]["items"] == [] if "items" in cards_sent[-1]["dev.diddy.card"] else True
    say("!stats")
    assert "Noch keine Statistik" in bot.sent[-1]
    bot._log_play(bot.player.current)                          # ohne Nutzer: nichts
    bot.player.requester = "@a:x"
    bot._log_play(bot.player.current)
    say("!stats")
    c = cards_sent[-1]["dev.diddy.card"]
    assert c["kind"] == "stats" and c["charts"][0]["items"][0] == {"label": "Song Eins", "value": 1}
    assert c["charts"][1]["items"][0]["label"] == "a"
    say("!fav play", "@b:x")                                   # abspielen steuert: nicht im Call
    assert "Call sind" in bot.sent[-1] or "Call" in bot.sent[-1]


def test_commands_are_published_once_per_room_and_failure_is_harmless(bot):
    calls = []

    async def get_state(room, evtype):
        return {}

    async def put_state(room, evtype, key, content):
        calls.append((room, evtype, key, content["prefix"]))

    bot.matrix.get_state_events = get_state
    bot.matrix.put_state = put_state
    in_call(bot, "!r", "@a:x")
    run(bot._on_text("!r", "@a:x", "!status"))
    run(bot._on_text("!r", "@a:x", "!status"))
    assert calls == [("!r", "dev.diddy.commands", "@bot:x", "!")]

    async def broken(*a, **k):
        raise RuntimeError("M_FORBIDDEN")

    bot.matrix.put_state = broken
    run(bot._on_text("!r2", "@a:x", "!status"))                # ohne Rechte: Befehl laeuft trotzdem
    assert bot.sent
