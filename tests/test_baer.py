"""Baer-DJ (Kamerabild ohne Video) und Ansage."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

import numpy as np
import pytest

av = pytest.importorskip("av")
pytest.importorskip("PIL")

from musikbot.ansage import spoken_title, text_for, to_stereo48k  # noqa: E402
from musikbot.baer import BaerDJ, Beat, BeatClock  # noqa: E402
from musikbot.config import Config  # noqa: E402
from musikbot.hub import Hub  # noqa: E402
from musikbot.player import DUCK, Player  # noqa: E402
from musikbot.store import TrackStore  # noqa: E402

SR = 48000


def _kick_track(bpm: float, seconds: float):
    """20-ms-Bloecke: kurzer 60-Hz-Schlag auf jedem Beat, dazwischen leises Rauschen."""
    rng = np.random.default_rng(0)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    beat = 60.0 / bpm
    env = np.exp(-((t % beat) / 0.05))
    sig = 0.8 * env * np.sin(2 * np.pi * 60 * t) + 0.01 * rng.standard_normal(n)
    pcm = (np.clip(sig, -1, 1) * 30000).astype(np.int16)
    for i in range(0, n - 960, 960):
        yield i / SR, np.repeat(pcm[i:i + 960], 2).reshape(1, -1)


def test_beat_finds_tempo_and_kicks():
    beat = Beat()
    for at, block in _kick_track(120, 12):
        beat.feed(block, 100.0 + at)
    st = beat.state(112.0)
    assert st["bpm"] == pytest.approx(120, abs=4)
    assert len(beat.kicks) >= 10
    assert beat.state(110.02)["loud"] > 0.2              # direkt nach einem Kick laut
    assert not beat.state(200.0)["audible"]              # lange nichts gehoert: Stille


def test_beat_clock_locks_to_kicks_and_runs_smoothly():
    beat, clock = Beat(), BeatClock()
    for at, block in _kick_track(120, 12):
        beat.feed(block, at)
    last = None
    steps = []
    for i in range(int(12 * 24)):                         # 24 Bilder/s
        now = i / 24
        pos = clock.advance(now, beat.state(now))
        if last is not None:
            steps.append(pos - last)
        last = pos
    assert clock.bpm == pytest.approx(120, abs=6)
    assert max(steps) < 0.2                               # keine Spruenge
    phase = (clock.advance(11.0, beat.state(11.0)) + 0.5) % 1 - 0.5
    assert abs(phase) < 0.2                               # Beat sitzt auf dem Kick


def test_beat_clock_half_time_for_fast_songs():
    beat, clock = Beat(), BeatClock()
    for at, block in _kick_track(160, 10):
        beat.feed(block, at)
    for i in range(240):
        clock.advance(i / 24, beat.state(i / 24))
    assert clock.song_bpm == pytest.approx(160, abs=6)
    assert clock.bpm == pytest.approx(80, abs=5)


def _pack(tmp_path, clips: dict, frames: int = 4) -> str:
    import json
    from PIL import Image
    for name in clips:
        for variant in ([name, name + "_open"] if clips[name].get("talk") else [name]):
            d = tmp_path / variant
            d.mkdir()
            for i in range(frames):
                Image.new("RGBA", (112, 140), (i * 40, 100, 200, 255)).save(d / f"{i:03d}.webp")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "fpb": 1, "beats": frames, "clips": clips,
        "units": {"top": 3.0, "bottom": -0.1, "desk": 0.85, "head": 2.5}}))
    return str(tmp_path)


def test_builtin_static_pack_renders_and_announces():
    baer = BaerDJ(426, 240)
    for at, block in _kick_track(128, 4):
        baer.beat.feed(block, at)
    cover = av.VideoFrame.from_ndarray(np.full((36, 64, 3), 200, np.uint8), format="rgb24")
    baer.set_track("Ein sehr langer Titel, der niemals in die Sprechblase passt, ehrlich", "Jemand", cover)
    baer.announce(1.0, np.full((1, SR * 2), 3000, np.int16))
    for now in (0.5, 1.5, 2.5, 3.9):
        frame = baer.render(now)
        assert (frame.width, frame.height) == (426, 240)
    assert baer.pack.static and baer.talking(1.5) and not baer.talking(5.0)
    assert baer._voice(1.5) > 0 and baer._voice(5.0) == 0


def test_frame_pack_loops_on_beat_and_lipsyncs(tmp_path):
    assets = _pack(tmp_path, {"mix": {"kind": "groove"}, "cheer": {"kind": "hype"},
                              "talk": {"kind": "talk", "talk": True}})
    baer = BaerDJ(256, 144, assets)
    baer.render(0.0)
    pack = baer.pack
    assert not pack.static and pack.talking == {"talk"}
    assert pack.frame_at("mix", 0.0).getpixel((5, 5))[0] == 0
    assert pack.frame_at("mix", 2.5).getpixel((5, 5))[0] == 80        # Bild 2 von 4
    assert pack.frame_at("mix", 4.0).getpixel((5, 5))[0] == 0         # Loop von vorn
    baer.announce(1.0, np.full((1, SR * 2), 9000, np.int16))
    baer.render(1.2)
    assert baer.director.clip == "talk"
    frame = baer.render(1.5)
    assert (frame.width, frame.height) == (256, 144)


def test_desk_pack_glows_with_the_bass(tmp_path):
    import json
    from PIL import Image
    assets = _pack(tmp_path, {"mix": {"kind": "groove"}})
    m = json.loads((tmp_path / "manifest.json").read_text())
    del m["units"]
    m["anchors"] = {"floor": 130.0, "head": 10.0}
    m["desk"] = {"size": [224, 140], "regions": {"front": [60, 100, 160, 130], "meter_0": [100, 80, 104, 95],
                                                 "jog_L": [40, 80, 70, 90], "edges": [20, 90, 200, 135]}}
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    (tmp_path / "desk").mkdir()
    for key, val in (("base", 20), ("glow", 250)):
        img = Image.new("RGBA", (224, 140), (0, 0, 0, 0))
        img.paste(Image.new("RGBA", (200, 50), (val, val, val, 255)), (12, 88))
        img.save(tmp_path / "desk" / f"{key}.png")
    baer = BaerDJ(256, 144, assets)
    baer.render(0.0)
    desk = baer.pack.desk
    assert desk is not None and set(desk["regions"]) == {"front", "meter_0", "jog_L", "edges"}
    fx = (desk["regions"]["front"][0] + desk["regions"]["front"][2]) // 2
    fy = (desk["regions"]["front"][1] + desk["regions"]["front"][3]) // 2
    quiet = baer.render(0.5).to_ndarray()[fy, fx, 0]
    for at, block in _kick_track(128, 3):
        baer.beat.feed(block, at)
    loud = max(int(baer.render(t).to_ndarray()[fy, fx, 0]) for t in (1.0, 1.5, 2.0, 2.5))
    assert loud > quiet + 40                                   # LED-Front leuchtet mit dem Bass


def test_director_jumps_to_hype_on_drop(tmp_path):
    assets = _pack(tmp_path, {"mix": {"kind": "groove"}, "cheer": {"kind": "hype"},
                              "jump": {"kind": "hype"}})
    baer = BaerDJ(256, 144, assets)
    baer.render(0.0)
    st = {"bass": 0.5, "loud": 0.5, "audible": True, "kicks": [], "bpm": 120.0, "drop_at": None}
    baer.director.update(1.0, 1.0, st, talking=False)
    assert baer.pack.kind[baer.director.clip] == "groove"
    baer.director.update(5.3, 3.0, dict(st, drop_at=2.9), talking=False)
    assert baer.pack.kind[baer.director.clip] == "hype"
    assert baer.director.start == 5.0                                 # ab dem naechsten Beat


def test_moderator_texts_vary_and_name_things():
    import random
    from musikbot.moderation import Moderator, who
    m = Moderator(rng=random.Random(3))
    texts = {m.between(("Voll", "suehnung"), ("Hemd an", "suehnung"), "@lndr:matrix.tllx.eu")
             for _ in range(8)}
    assert len(texts) >= 6
    assert all("Hemd an" in t for t in texts)
    assert any("lndr" in t for t in texts)
    assert "Voll" in m.end(("Voll", ""))
    assert "Hemd an" in m.start(("Hemd an", ""), first=True)
    assert who("@lndr:matrix.tllx.eu") == "lndr" and who("autoplay") == ""


def test_spoken_title_cleans_youtube_noise():
    assert spoken_title("Daft Punk - One More Time (Official Video)", "Daft Punk - Topic") == \
        ("One More Time", "Daft Punk")
    assert spoken_title("Song [Lyrics] | 4K", "Kanal") == ("Song", "Kanal")
    assert spoken_title("", "") == ("der nächste Titel", "")
    assert text_for("Voll", "suehnung") == "Jetzt kommt: Voll von suehnung!"


def test_to_stereo48k_pads_and_resamples():
    mono = np.full(22050, 1000, np.int16)                 # 1 s bei 22,05 kHz
    out = to_stereo48k(mono, 22050)
    assert out.dtype == np.int16 and out.shape[0] == 1
    assert out.shape[1] / 2 / SR == pytest.approx(1.0 + 0.3 + 0.5, abs=0.05)


def _player(**kw) -> Player:
    d = tempfile.mkdtemp()
    cfg = Config(homeserver="http://h", user="@b:x", password="p", data_dir=d,
                 matrix_user_id="@b:x", ansage_voice="", **kw)
    return Player(cfg, Hub(), TrackStore(d + "/db", d + "/tmp"))


def test_mix_voice_ducks_music_and_ends():
    p = _player()
    p._voice = np.full((1, 960 * 2 * 50), 1000, np.int16)      # 1 s Stimme
    p._voice_i = 0
    music = np.full((1, 960 * 2), 10000, np.int16)
    out = [p._mix_voice(music.copy()) for _ in range(50)]
    mid = out[25]
    assert int(mid[0, 0]) == pytest.approx(10000 * DUCK + 1000, abs=20)
    assert p._voice is None                                     # danach wieder normal
    assert int(out[0][0, 0]) == pytest.approx(10000 + 1000, abs=50)   # weicher Einstieg


def test_baer_replaces_cover_and_streams_frames():
    asyncio.run(_baer_run())


async def _baer_run():
    p = _player(video_height=144, video_fps=20)
    assert p.baer is not None and p.baer.w == 256
    sink = p.hub.new_sink()
    p.current = SimpleNamespace(key="k1", title="Titel", uploader="")
    p._source = SimpleNamespace(has_video=False)
    p.set_cover("k1", None)
    assert sink.take_video() is None                            # kein Titelbild, der Baer kommt
    task = asyncio.create_task(p._baer_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    frame = sink.take_video()
    assert frame is not None and (frame.width, frame.height) == (256, 144)
    p._source = SimpleNamespace(has_video=True)                 # auch bei echtem Video: Baer
    assert p._baer_active()
    p.cfg.baer_over_video = False                               # Schalter aus: Video schlaegt Baer
    assert not p._baer_active()


def test_without_video_or_disabled_no_baer():
    assert _player(baer_enabled=False).baer is None
    p = _player(enable_video=False)
    p.hub.new_sink()
    p._source = SimpleNamespace(has_video=False)
    assert not p._baer_active()


@pytest.mark.skipif(not os.path.exists(os.environ.get("ANSAGE_TEST_VOICE", "/nope")),
                    reason="Piper-Stimme fuer den Test fehlt (ANSAGE_TEST_VOICE)")
def test_piper_synth_real_voice():
    from musikbot.ansage import Ansager
    a = Ansager(os.environ["ANSAGE_TEST_VOICE"])
    out = a.synth(text_for("Voll", "suehnung"))
    assert out is not None and out.shape[1] / 2 / SR > 1.0
    assert np.abs(out).max() > 1000


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg fehlt")
def test_baer_over_video_switch():
    vid = SimpleNamespace(has_video=True)
    p = _player()
    p._source = vid
    p.hub.new_sink()
    assert not p._shows_video(vid) and p._baer_active()        # Standard: Baer statt YouTube-Video
    off = _player(baer_over_video=False)
    off._source = vid
    off.hub.new_sink()
    assert off._shows_video(vid) and not off._baer_active()
    assert _player(baer_enabled=False)._shows_video(vid)       # ohne Baer: Video wie frueher


def test_night_greeting_is_evening():
    import datetime as dt
    from musikbot import moderation
    m = moderation.Moderator()

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 11, 22, 0, 39, tzinfo=tz)
    orig, moderation.dt.datetime = moderation.dt.datetime, _DT
    try:
        assert m._vars()["gruss"] == "Abend"
    finally:
        moderation.dt.datetime = orig


def test_embedded_cover_is_still_not_video(tmp_path):
    import subprocess
    from musikbot.media import MediaSource
    pic, mp3 = tmp_path / "c.png", tmp_path / "s.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=300x300", "-frames:v", "1",
                    str(pic)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", "-i", str(pic),
                    "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                    "-c:a", "libmp3lame", str(mp3)], check=True)
    src = MediaSource(str(mp3), video_height=144)
    src.open()
    try:
        assert not src.has_video
        assert src.still is not None and (src.still.width, src.still.height) == (256, 144)
        assert sum(b.shape[1] for b in src.audio_blocks()) > 0
    finally:
        src.close()


class _FakeAnsager:
    available = True

    def synth(self, text):
        self.text = text
        return np.full((1, SR * 2 // 2), 4000, np.int16)      # 0,5 s


def _tone_file(path, seconds=1.0):
    with av.open(path, mode="w") as box:
        stream = box.add_stream("aac", rate=SR)
        for start in range(0, int(seconds * SR), 1024):
            val = (3000 * np.sin(2 * np.pi * 440 * (start + np.arange(1024)) / SR)).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(np.repeat(val, 2).reshape(1, -1), format="s16", layout="stereo")
            frame.sample_rate, frame.pts = SR, start
            for pkt in stream.encode(frame):
                box.mux(pkt)
        for pkt in stream.encode():
            box.mux(pkt)


def _mod_player(tmp_path):
    from musikbot.store import Track
    p = _player(enable_video=False)
    for key in ("a", "b"):
        path = str(tmp_path / f"{key}.m4a")
        _tone_file(path)
        p.store._tracks[key] = Track(key=key, title=f"Titel {key}", uploader="Band", abs_path=path)
    p.store.release_file = lambda key: None
    p.ansager = _FakeAnsager()
    return p


def test_moderation_pause_speaks_then_plays_next(tmp_path):
    async def run():
        p = _mod_player(tmp_path)
        sink = p.hub.new_sink()
        p.queue.append(("b", "@lndr:x"))
        ok = await p._advance(auto=True, prev=p.store.get("a"))
        assert ok and p.current.key == "b"
        assert "Titel b" in p.ansager.text
        assert len(sink.audio) >= 20                         # Stimme ging raus (ohne Musik)
        assert p._no_intro_key is None                       # Intro nicht nochmal angesagt
        await p.shutdown()
    asyncio.run(run())


def test_baer_keeps_rendering_during_moderation(tmp_path):
    async def run():
        p = _mod_player(tmp_path)
        p.video_enabled = True
        p.baer = BaerDJ(160, 90)
        p.hub.new_sink()
        p.queue.append(("b", ""))
        seen = []
        task = asyncio.create_task(p._advance(auto=True, prev=p.store.get("a")))
        while not task.done():
            seen.append(p._baer_active())
            await asyncio.sleep(0.05)
        assert any(seen)                                     # Pause ohne Quelle: Bild laeuft weiter
        assert not p._speaking
        await p.shutdown()
    asyncio.run(run())


def test_moderation_aborts_on_stop(tmp_path):
    async def run():
        p = _mod_player(tmp_path)
        p.hub.new_sink()
        p.queue.append(("b", ""))
        task = asyncio.create_task(p._advance(auto=True, prev=p.store.get("a")))
        await asyncio.sleep(0.15)
        await p.stop(clear_queue=True)
        assert await task is False
        assert p.current is None or p.current.key != "b"
        await p.shutdown()
    asyncio.run(run())


def test_moderation_replaces_crossfade():
    p = _player()
    assert not p.moderation_on                             # ohne Stimme: alles wie gehabt
    p.ansager = _FakeAnsager()
    assert p.moderation_on
    off = _player(moderation_enabled=False)
    off.ansager = _FakeAnsager()
    assert not off.moderation_on


def test_publikum_avatars_and_identities():
    import io
    from PIL import Image
    from musikbot.publikum import round_avatar, user_of
    assert user_of("@lndr:matrix.tllx.eu:ABCDEF") == "@lndr:matrix.tllx.eu"
    assert user_of("@lndr:matrix.tllx.eu") == "@lndr:matrix.tllx.eu"
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 30, 90)).save(buf, "PNG")
    av_img = round_avatar(buf.getvalue(), 64)
    assert av_img.size == (64, 64) and av_img.getpixel((0, 0))[3] == 0 and av_img.getpixel((32, 32))[3] == 255
    assert round_avatar(b"kein bild") is None


def test_audience_drawn_in_background():
    baer = BaerDJ(256, 144)
    baer.render(0.0)
    plain = baer.render(0.5).to_ndarray()
    from PIL import Image
    face = Image.new("RGBA", (40, 40), (0, 255, 0, 255))
    baer.set_audience([("Luis", face), ("Lndr", None)])
    with_faces = baer.render(1.0).to_ndarray()
    x, y = int(256 * 0.13), int(144 * 0.47)
    assert with_faces[y, x, 1] > 200 and plain[y, x, 1] < 200      # gruener Avatar links
    baer.set_audience([])
    assert baer._audience == []


def test_beat_reports_frequency_bands():
    b = Beat()
    for at, block in _kick_track(120, 2):
        b.feed(block, at)
    bands = b.state(1.5)["bands"]
    assert len(bands) == 12 and float(np.max(bands)) <= 1.0
    assert b.state(10.0)["bands"].sum() == 0                    # Stille: alles aus


def test_element_call_audience_order():
    from musikbot.elementcall import ElementCall
    ec = ElementCall.__new__(ElementCall)
    ec.cfg = SimpleNamespace(matrix_user_id="@music:x", user="@music:x", device_id="BOT")
    ec._lk = SimpleNamespace(remote_participants={
        "a": SimpleNamespace(identity="@luis:x:D1"), "b": SimpleNamespace(identity="@lndr:x:D2"),
        "c": SimpleNamespace(identity="@luis:x:D3")})
    ec.members = {}
    assert ec.audience("!r") == ["@luis:x", "@lndr:x", "@music:x"]


def test_zuruf_names_people_in_call():
    import random
    from musikbot.moderation import DROP, Moderator
    m = Moderator(rng=random.Random(3))
    texts = [m.zuruf(["Lndr", "Luis"]) for _ in range(30)]
    assert any("Lndr" in t or "Luis" in t for t in texts)
    assert all("{" not in t for t in texts)
    assert m.zuruf([], drop=True) in DROP
    assert all("{name}" not in m.zuruf([]) for _ in range(10))


def test_shout_over_running_song(tmp_path):
    async def run():
        p = _mod_player(tmp_path)
        p.baer = BaerDJ(160, 90)
        p.hub.new_sink()
        p.set_audience([("Lndr", None), ("music", None)], ["Lndr"])
        assert p._listeners == ["Lndr"]
        await p.play(p.store.get("a"), "@lndr:x")
        assert not p._zuruf_ok()                             # Titelanfang: nicht reinrufen
        p._voice = None
        p._pos = 30.0
        p._source.duration = 120.0
        assert p._zuruf_ok()
        assert await p.shout("Lndr, du bist geil!")
        assert p._voice is not None and p.baer._bubble[1] == "Lndr, du bist geil!"
        assert not p._zuruf_ok()                             # waehrend er redet: kein zweiter Zuruf
        p._voice = None
        p._pos = 110.0
        assert not p._zuruf_ok()                             # kurz vor Schluss: auch nicht
        await p.shutdown()
    asyncio.run(run())
