"""Blick: Kamera/Bildschirm anschauen und kommentieren – mit Attrappen, ohne Netz."""
from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

from PIL import Image

from musikbot.blick import SKIP, Blick, fingerprint, same_image
from musikbot.elementcall import remote_key_targets
from musikbot.ki import KI, clean_line


class FakeKI(KI):
    def __init__(self, answers):
        super().__init__("http://ki", "", "m")
        self.answers = list(answers)
        self.seen = []

    async def chat(self, messages, max_tokens=120, temperature=0.9):
        self.seen.append(messages)
        return self.answers.pop(0) if self.answers else None


class FakeCall:
    def __init__(self, videos, img=None):
        self.videos = list(videos)
        self.img = img or Image.new("RGB", (640, 360), (40, 120, 200))
        self.shots = []

    def remote_videos(self):
        return list(self.videos)

    async def snapshot(self, ident, kind, timeout=10.0):
        self.shots.append((ident, kind))
        return self.img


class FakePlayer:
    def __init__(self, ok=True):
        self.ok = ok
        self.said = []
        self.current = SimpleNamespace(key="k")
        self.speech_idle = 999.0
        self.state = "playing"

    def _zuruf_ok(self):
        return self.ok

    def _info(self, cur):
        return ("Voll", "Künstler")

    async def shout(self, text):
        self.said.append(text)
        return True

    def react(self, event, seconds=2.5, **kw):
        self.reactions = getattr(self, "reactions", []) + [(event, kw.get("after_talk", False))]


def make(videos, answers, allowed=lambda u: True, img=None):
    cfg = SimpleNamespace(dj_name="DJ Qwen", blick_every=180.0)
    ki = FakeKI(answers)
    call = FakeCall(videos, img)
    player = FakePlayer()
    b = Blick(cfg, ki, call, player, name_of=lambda i: i.split(":")[0].lstrip("@").title(),
              allowed=allowed, rng=random.Random(1))
    return b, ki, call, player


def test_all_sources_at_once_screens_first_capped():
    vids = [("@max:x:D1", "camera"), ("@anna:x:D2", "screen")] + [(f"@p{i}:x:D", "camera") for i in range(8)]
    b, *_ = make(vids, [])
    c = b.candidates()
    assert c[0] == ("@anna:x:D2", "screen") and len(c) == 6


def test_opted_out_people_are_never_looked_at():
    vids = [("@max:x:D1", "camera"), ("@anna:x:D2", "screen")]
    b, *_ = make(vids, [], allowed=lambda u: u != "@anna:x")
    assert b.candidates() == [("@max:x:D1", "camera")]


async def test_one_request_with_everyone_small_and_compressed():
    import base64
    import io
    vids = [("@max:x:D1", "camera"), ("@anna:x:D2", "screen")]
    b, ki, call, player = make(vids, ["„Max und Anna, ihr seid ja fleißig! 🎧“"],
                               img=Image.new("RGB", (1920, 1080), (40, 120, 200)))
    assert await b.look()
    assert sorted(call.shots) == sorted(vids)                    # beide Standbilder geholt
    assert len(ki.seen) == 1                                     # aber nur EINE Anfrage
    assert player.said == ["Max und Anna, ihr seid ja fleißig!"]
    sys_msg, user_msg = ki.seen[0]
    assert "DJ Qwen" in sys_msg["content"] and "Aussehen" in sys_msg["content"]
    parts = user_msg["content"]
    texts = " ".join(p["text"] for p in parts if p["type"] == "text")
    assert "Bild 1: geteilter Bildschirm von Anna (neu)" in texts and "Bild 2: Kamerabild von Max (neu)" in texts
    assert "Voll von Künstler" in texts
    imgs = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert len(imgs) == 2
    sizes = [Image.open(io.BytesIO(base64.b64decode(u.split(",", 1)[1]))).size for u in imgs]
    assert sizes == [(640, 360), (448, 252)]                     # Bildschirm groesser, Kamera klein


async def test_missing_snapshots_are_skipped():
    b, ki, call, player = make([("@max:x:D1", "camera"), ("@anna:x:D2", "camera")], ["Max, hallo!"])

    async def snap(ident, kind, timeout=10.0):
        if ident.startswith("@anna"):
            raise RuntimeError("kaputt")
        return call.img
    call.snapshot = snap
    assert await b.look()
    texts = " ".join(p.get("text", "") for p in ki.seen[0][1]["content"])
    assert "Max" in texts and "Anna" not in texts and player.said == ["Max, hallo!"]


async def test_nothing_to_see_means_silence():
    b, ki, call, player = make([("@max:x:D1", "camera")], [SKIP, "-  ", "ok"])
    for _ in range(3):
        await b.look()
        b._last_print.clear()
    assert player.said == []


async def test_unchanged_pictures_are_not_commented_twice():
    b, ki, call, player = make([("@max:x:D1", "screen")], ["Max, wieder am Coden?", "Max, immer noch?"])
    await b.look()
    await b.look()
    assert player.said == ["Max, wieder am Coden?"]
    assert len(ki.seen) == 1
    call.img = Image.new("RGB", (640, 360), (250, 240, 30))   # neues Bild: wieder Spruch, mit Hinweis
    await b.look()
    assert player.said[-1] == "Max, immer noch?"
    last = " ".join(p.get("text", "") for p in ki.seen[-1][1]["content"])
    assert "letzten Sprüche (sag etwas anderes): Max, wieder am Coden?" in last


async def test_waits_while_dj_is_busy_and_drops_line_when_cameras_went_off():
    b, ki, call, player = make([("@max:x:D1", "camera")], ["Max, hallo!"])
    player.ok = False
    assert await b.look() is False and call.shots == []
    player.ok = True

    async def gone(*a, **k):
        call.videos.clear()
        return "Max, hallo!"
    ki.chat = gone
    assert await b.look() is True
    assert player.said == []


async def test_manual_look_comments_even_unchanged_pictures_and_says_it():
    b, ki, call, player = make([("@max:x:D1", "camera")], ["Max, hi!", "Max, schon wieder du!"])
    assert await b.look()
    assert await b.look_now() == ("said", "Max, schon wieder du!")     # gleiches Bild, trotzdem
    assert player.said == ["Max, hi!", "Max, schon wieder du!"] and len(ki.seen) == 2
    assert player.reactions == [("blick", False), ("blick_lachen", True)] * 2   # neugierig, nach dem Spruch lachen
    texts = " ".join(p["text"] for p in ki.seen[1][1]["content"] if p["type"] == "text")
    assert "Deine letzten Sprüche (sag etwas anderes): Max, hi!" in texts


async def test_manual_look_without_music_or_gap_goes_to_chat(monkeypatch):
    import musikbot.blick as bl
    b, ki, call, player = make([("@max:x:D1", "camera")], ["Max, hi!", "Max, Lampe an!"])
    player.state = "paused"
    assert await b.look_now() == ("text", "Max, hi!") and player.said == []
    player.state, player.ok = "playing", False                       # Musik laeuft, aber Ansage/Songanfang
    monkeypatch.setattr(bl, "SAY_WAIT", 0.1)
    assert await b.look_now() == ("text", "Max, Lampe an!") and player.said == []


async def test_manual_look_at_one_person_respects_opt_out():
    vids = [("@max:x:D1", "camera"), ("@anna:x:D2", "screen")]
    b, ki, call, player = make(vids, ["Anna, fleißig!"], allowed=lambda u: u != "@max:x")
    assert await b.look_now(lambda i: i.startswith("@max")) == ("nobody", None)
    assert await b.look_now(lambda i: i.startswith("@anna")) == ("said", "Anna, fleißig!")
    assert call.shots == [("@anna:x:D2", "screen")]


async def test_manual_look_reports_busy_nothing_and_missing_picture():
    b, ki, call, player = make([("@max:x:D1", "camera")], [SKIP])
    async with b._lock:
        assert b.busy and await b.look_now() == ("busy", None)
        assert await b.look()                                      # Schleife wartet einfach die naechste Runde ab
    assert await b.look_now() == ("nothing", None)
    call.img = None
    assert await b.look_now() == ("noimage", None)
    assert player.said == []


def test_fingerprint_detects_same_picture():
    a = Image.new("RGB", (320, 180), (10, 10, 10))
    b = Image.new("RGB", (640, 360), (12, 10, 10))
    c = Image.new("RGB", (640, 360), (200, 200, 200))
    assert same_image(fingerprint(a), fingerprint(b))
    assert not same_image(fingerprint(a), fingerprint(c))


def test_clean_line_makes_it_speakable():
    assert clean_line('**„Hey Max!“** 🐻🎧') == "Hey Max!"
    long = " ".join(["wort"] * 40)
    assert clean_line(long, max_words=5) == "wort wort wort wort wort…"


def test_remote_keys_only_for_the_sender_himself():
    import base64
    key = base64.b64encode(b"k" * 32).decode().rstrip("=")
    ids, keys = remote_key_targets("@max:x", "DEV1", {
        "keys": {"index": 3, "key": key},
        "member": {"id": "@max:x:DEV1", "claimed_device_id": "DEV1"}})
    assert ids == {"@max:x:DEV1"} and keys == [(3, b"k" * 32)]
    ids, keys = remote_key_targets("@max:x", None, {        # altes Format, Geraet nur behauptet
        "keys": [{"index": 0, "key": key}], "device_id": "DEV2",
        "member": {"id": "@anna:x:DEV9"}})                   # fremde Identity: ignoriert
    assert ids == {"@max:x:DEV2"} and keys == [(0, b"k" * 32)]


async def test_rtc_key_hook_feeds_the_key_provider():
    """Der Olm-Hook reicht Element-Call-Schluessel an ElementCall weiter, der setzt sie im LiveKit-Keyring."""
    import base64

    from musikbot.elementcall import ElementCall
    ec = ElementCall.__new__(ElementCall)
    ec._remote_keys = {}
    ec._key = b"x" * 32
    got = []
    ec._lk = SimpleNamespace(e2ee_manager=SimpleNamespace(key_provider=SimpleNamespace(
        set_key=lambda ident, key, idx: got.append((ident, key, idx)))))
    ec._on_rtc_key("@max:x", "DEV1", {"keys": [{"index": 1, "key": base64.b64encode(b"a" * 32).decode()}]})
    assert got == [("@max:x:DEV1", b"a" * 32, 1)]
    ec._lk = None                                    # vor dem Beitritt: merken, spaeter setzen
    ec._on_rtc_key("@anna:x", "DEV2", {"keys": [{"index": 0, "key": base64.b64encode(b"b" * 32).decode()}]})
    ec._lk = SimpleNamespace(e2ee_manager=SimpleNamespace(key_provider=SimpleNamespace(
        set_key=lambda ident, key, idx: got.append((ident, key, idx)))))
    ec._apply_remote_keys()
    assert ("@anna:x:DEV2", b"b" * 32, 0) in got


def test_olm_hook_catches_element_call_keys():
    from nio.crypto import Olm

    from musikbot import matrix
    calls = []
    matrix.on_rtc_key(lambda s, d, c: calls.append((s, d, c)))
    fake = SimpleNamespace(device_store=SimpleNamespace(active_user_devices=lambda u: [
        SimpleNamespace(id="DEVX", curve25519="CURVE")]))
    out = Olm._handle_olm_event(fake, "@max:x", "CURVE",
                                {"type": matrix.RTC_KEYS_EVENT, "content": {"keys": []}})
    assert out is None and calls[-1] == ("@max:x", "DEVX", {"keys": []})
    matrix._rtc_key_listeners.remove(matrix._rtc_key_listeners[-1])


async def test_loop_runs_and_stops_cleanly():
    b, ki, call, player = make([("@max:x:D1", "camera")], ["Max, hi!"])
    b.cfg.blick_every = 0.01
    import musikbot.blick as bl
    old = bl.FIRST_LOOK
    bl.FIRST_LOOK = (0.0, 0.0)
    try:
        b.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if player.said:
                break
        assert player.said == ["Max, hi!"]
    finally:
        bl.FIRST_LOOK = old
        b.stop()
    assert not b.running and not b._lines


# ---------------------------------------------------------------- im Bot: Schalter, Widerspruch, Hinweis
def _ki_bot(tmp_path, **kw):
    from musikbot.bot import Bot
    from musikbot.config import Config
    cfg = Config(homeserver="http://h", user="@bot:x", password="p", data_dir=str(tmp_path / "d"),
                 matrix_user_id="@bot:x", dj_name="DJ Qwen", ki_url="http://ki/v1", ki_model="m",
                 **kw)
    bot = Bot(cfg)
    notes = []

    async def note(room, text, tone="info", icon=""):
        notes.append(text)
    bot._note = note
    return bot, notes


async def test_everyone_can_opt_out_for_themselves_and_it_sticks(tmp_path):
    from musikbot.commands import is_read_only
    bot, notes = _ki_bot(tmp_path, blick_enabled=True)
    assert bot.blick is not None
    assert is_read_only("blick", "aus")                                   # auch wer nicht im Call ist
    await bot._dispatch("!r:x", "@Max:x", "blick", "aus")
    assert not bot._ki_allowed("@max:x") and bot._ki_allowed("@anna:x")
    assert "für dich aus" in notes[-1]
    again, notes2 = _ki_bot(tmp_path, blick_enabled=True)                 # nach Neustart noch da
    assert not again._ki_allowed("@max:x")
    await again._dispatch("!r:x", "@max:x", "blick", "status")
    assert "allgemein an, für dich aus" in notes2[-1]
    await again._dispatch("!r:x", "@max:x", "blick", "an")
    assert again._ki_allowed("@max:x")


async def test_off_by_default_and_join_tells_the_room_once(tmp_path):
    bot, notes = _ki_bot(tmp_path)
    assert bot.blick is None
    bot._ki_joined("!r:x")
    await asyncio.sleep(0)
    assert notes == []
    bot, notes = _ki_bot(tmp_path / "2", blick_enabled=True)
    bot.blick.start = lambda: None
    bot._ki_joined("!r:x")
    bot._ki_joined("!r:x")
    await asyncio.sleep(0.01)
    assert len(notes) == 1
    assert "Kameras" in notes[0] and "!blick aus" in notes[0] and "Nichts davon wird gespeichert" in notes[0]


async def test_blick_command_looks_now_for_people_in_the_call(tmp_path):
    from musikbot.commands import is_read_only
    assert is_read_only("blick", "aus") and is_read_only("blick", "status")
    assert not is_read_only("blick", "") and not is_read_only("blick", "mich")   # hinschauen: nur wer im Call ist
    bot, notes = _ki_bot(tmp_path, blick_enabled=True)
    await bot._dispatch("!r:x", "@max:x", "blick", "")
    assert "keinem Call" in notes[-1]
    bot.ecall.room_id = "!r:x"
    seen = []

    async def look_now(match=None):
        seen.append(match)
        return "text", "Max, schicke Lampe!"
    bot.blick.look_now = look_now
    await bot._dispatch("!r:x", "@Max:x", "blick", "mich")
    await asyncio.gather(*list(bot._bg_tasks))
    assert notes[-2].startswith("Moment") and notes[-1] == "DJ Qwen: Max, schicke Lampe!"
    assert seen[0]("@max:x:DEV") and not seen[0]("@anna:x:DEV")
    await bot._dispatch("!r:x", "@max:x", "blick", "anna")
    await asyncio.gather(*list(bot._bg_tasks))
    assert seen[1]("@anna:x:DEV") and not seen[1]("@max:x:DEV")
    await bot._dispatch("!r:x", "@max:x", "blick", "")
    await asyncio.gather(*list(bot._bg_tasks))
    assert seen[2] is None
    await bot._dispatch("!r:x", "@max:x", "blick", "aus")
    await bot._dispatch("!r:x", "@max:x", "blick", "mich")
    assert "erst !blick an" in notes[-1] and len(seen) == 3
